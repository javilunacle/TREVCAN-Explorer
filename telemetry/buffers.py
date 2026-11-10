"""Bounded RAM batching buffers. Outages leave gaps instead of disk backlogs."""

import time
import uuid
from collections import OrderedDict, deque

from .common import validate_frame


# These buffers store complete per-frame records in RAM, including decoded
# signals when present. They never combine samples into latest signal values.
# CarBuffer runs on the car before TCP sending; RawBuffer runs on the receiving
# Pi before downstream export. FrameBuffer supplies their shared size/age limits.


class FrameBuffer:
    def __init__(self, *, max_frames=4096, max_age_seconds=1):
        self.max_frames = max_frames
        self.max_age_seconds = max_age_seconds
        # Each deque entry is (local expiry time, complete frame dictionary).
        # Expiry uses monotonic time so wall-clock adjustments do not affect it.
        self.frames = deque()
        self.dropped = 0

    def _trim(self):
        # Frames enter in order, so expired entries can be removed from the
        # front until the oldest remaining frame is still within its RAM lifetime.
        now = time.monotonic()
        while self.frames and self.frames[0][0] <= now:
            self.frames.popleft()
            self.dropped += 1

    def _offer(self, frame):
        self._trim()
        # Make room by dropping the oldest sample when the buffer reaches its
        # capacity. This keeps capture fresh instead of growing an old backlog.
        if len(self.frames) >= self.max_frames:
            self.frames.popleft()
            self.dropped += 1
        self.frames.append((time.monotonic() + self.max_age_seconds, frame))

    def pending(self, limit=5000):
        self._trim()
        # Return a limited batch without removing it. The caller later confirms
        # completion with an ACK (car) or finish_export (receiving Pi).
        return [frame for _, frame in list(self.frames)[:limit]]

    def discard_pending(self):
        # Outages discard pending samples and count them as drops. Restarting a
        # connection does not replay these frames from RAM or any disk storage.
        self.dropped += len(self.frames)
        self.frames.clear()

    def close(self):
        # Shutdown releases the RAM contents; nothing is persisted for restart.
        self.frames.clear()


class CarBuffer(FrameBuffer):
    def __init__(self, stream_id=None, **kwargs):
        super().__init__(**kwargs)
        # A process restart begins a new stream; no sequence checkpoint is kept.
        # stream_id is an optional prefix, not a fixed vehicle identity. The
        # UUID suffix keeps restarted sequence numbers distinct at the receiver.
        epoch = uuid.uuid4().hex
        self.stream_id = f"{stream_id[:95]}-{epoch}" if stream_id else epoch
        # Sequences belong to this process's stream. They increase for every
        # valid capture record, including samples discarded while disconnected.
        self.next_seq = 1
        self.last_timestamp_ns = self.last_acked = 0
        self.connected = False

    def set_connected(self, connected):
        # send_forever updates this when its off-car TCP connection opens/closes.
        self.connected = connected
        if not connected:
            self.discard_pending()

    def append_many(self, records):
        sequences = []
        for record in records:
            if len(record) not in (9, 10, 11):
                raise ValueError("invalid capture record")
            # Standalone capture keeps its original tuple. CANManager adds its
            # decoded dictionary and the metadata identifying that same decode.
            bus, can_id, data, extended, remote, fd, error, dlc, timestamp = record[:9]
            data = bytes(data)
            # Build the outgoing record here so raw fields and decoded signals
            # share one stream identity and sequence. The timestamp is kept
            # strictly increasing even if capture times repeat or move backward.
            frame = {"type": "frame", "stream_id": self.stream_id, "seq": self.next_seq,
                     "timestamp_ns": max(timestamp or time.time_ns(), self.last_timestamp_ns + 1),
                     "bus": bus, "can_id": can_id, "is_extended": bool(extended),
                     "is_remote": bool(remote), "is_fd": bool(fd), "is_error": bool(error),
                     "dlc": len(data) if dlc is None else dlc, "data": data.hex().upper()}
            if len(record) >= 10:
                frame["decoded"] = record[9]
            if len(record) == 11:
                for name in ("dbc", "message", "labels"):
                    frame[name] = record[10][name]
            # Validate before consuming a sequence or adding this frame to RAM.
            validate_frame(frame)
            self.last_timestamp_ns = frame["timestamp_ns"]
            self.next_seq += 1
            sequences.append(frame["seq"])
            # Disconnected captures still advance the sequence. The resulting
            # gaps tell the receiver samples were dropped without renumbering.
            if self.connected:
                self._offer(frame)
            else:
                self.dropped += 1
        return sequences

    def append(self, can_id, data, *, bus="can0", is_extended=False, is_remote=False,
               is_fd=False, is_error=False, dlc=None, timestamp_ns=None, decoded=None):
        # Single-frame callers use the same validation and sequence assignment
        # as batched capture. An empty decoded dictionary is a valid paired frame.
        record = (bus, can_id, data, is_extended, is_remote, is_fd,
                  is_error, dlc, timestamp_ns)
        if decoded is not None:
            record += (decoded,)
        return self.append_many([record])[0]

    def acknowledge_through(self, seq):
        # ACKs are cumulative: acknowledging N releases every buffered frame
        # through N. A repeated ACK is allowed, but an unassigned sequence is not.
        if not self.last_acked <= seq < self.next_seq:
            raise ValueError("unexpected ACK sequence")
        self.last_acked = seq
        while self.frames and self.frames[0][1]["seq"] <= seq:
            self.frames.popleft()

    def status(self):
        # Trim first so the reported queue length excludes already expired data.
        self._trim()
        return {"storage": "ram", "stream_id": self.stream_id, "queued": len(self.frames),
                "connected": self.connected, "dropped": self.dropped,
                "last_acked": self.last_acked, "next_seq": self.next_seq}


class RawBuffer(FrameBuffer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Track the last accepted sequence per recent stream independently of
        # export progress, so a retransmitted batch can be ACKed without storing it twice.
        self.streams = OrderedDict()
        self.received = 0

    def last_sequence(self, stream_id):
        return self.streams.get(stream_id, 0)

    def commit_batch(self, frames):
        """Return the ACK sequence and new frames after atomic validation."""
        # Check the complete batch before offering any records to the buffer.
        # Each batch must contain one stream with strictly increasing sequences.
        if not isinstance(frames, list) or not frames or len(frames) > 4096:
            raise ValueError("invalid frame batch")
        validated = [(frame, validate_frame(frame)) for frame in frames]
        stream_id = frames[0]["stream_id"]
        if any(frame["stream_id"] != stream_id for frame in frames):
            raise ValueError("batch contains multiple stream identities")
        if any(right["seq"] <= left["seq"] for left, right in zip(frames, frames[1:])):
            raise ValueError("batch sequences must increase")
        last_seq = self.last_sequence(stream_id)
        accepted = []
        for frame, payload in validated:
            if frame["seq"] <= last_seq:
                continue  # A lost ACK may cause one batch to arrive again.
            # Keep all received fields, including decoded, and convert the raw
            # hex payload into bytes for downstream workers on the receiving Pi.
            captured = {**frame, "data": payload}
            self._offer(captured)
            # The receiver prepares these same dictionaries before yielding to
            # either output worker. Duplicate sequences never calculate twice.
            accepted.append(captured)
            self.received += 1
        # Remember acceptance even when old buffered frames expire or are
        # dropped. Bound this deduplication history to the 128 most recent streams.
        self.streams[stream_id] = max(last_seq, frames[-1]["seq"])
        self.streams.move_to_end(stream_id)
        if len(self.streams) > 128:
            self.streams.popitem(last=False)
        # The network receiver uses this as its ACK: RAM acceptance only.
        return frames[-1]["seq"], accepted

    def commit_frame(self, frame):
        return self.commit_batch([frame])

    def finish_export(self, frames):
        # Remove only the exact stream/sequence pairs from the completed export.
        # New frames accepted while that export was running must remain queued.
        exported = {(frame["stream_id"], frame["seq"]) for frame in frames}
        self.frames = deque((expiry, frame) for expiry, frame in self.frames
                            if (frame["stream_id"], frame["seq"]) not in exported)

    def status(self):
        self._trim()
        # received is cumulative; pending_influx is just the current RAM backlog.
        return {"storage": "ram", "raw_frames": self.received,
                "pending_influx": len(self.frames), "dropped": self.dropped,
                "streams": dict(self.streams)}

    quick_status = status
