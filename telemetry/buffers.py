"""Bounded RAM batching buffers. Outages leave gaps instead of disk backlogs."""

import time
import uuid
from collections import OrderedDict, deque

from .common import validate_frame


class FrameBuffer:
    def __init__(self, *, max_frames=4096, max_age_seconds=1):
        self.max_frames = max_frames
        self.max_age_seconds = max_age_seconds
        self.frames = deque()
        self.dropped = 0

    def _trim(self):
        now = time.monotonic()
        while self.frames and self.frames[0][0] <= now:
            self.frames.popleft()
            self.dropped += 1

    def _offer(self, frame):
        self._trim()
        if len(self.frames) >= self.max_frames:
            self.frames.popleft()
            self.dropped += 1
        self.frames.append((time.monotonic() + self.max_age_seconds, frame))

    def pending(self, limit=5000):
        self._trim()
        return [frame for _, frame in list(self.frames)[:limit]]

    def discard_pending(self):
        self.dropped += len(self.frames)
        self.frames.clear()

    def close(self):
        self.frames.clear()


class CarBuffer(FrameBuffer):
    def __init__(self, car_id=None, **kwargs):
        super().__init__(**kwargs)
        # A process restart begins a new stream; no sequence checkpoint is kept.
        epoch = uuid.uuid4().hex
        self.car_id = f"{car_id[:95]}-{epoch}" if car_id else epoch
        self.next_seq = 1
        self.last_timestamp_ns = self.last_acked = 0
        self.connected = False

    def set_connected(self, connected):
        self.connected = connected
        if not connected:
            self.discard_pending()

    def append_many(self, records):
        sequences = []
        for bus, can_id, data, extended, remote, fd, error, dlc, timestamp in records:
            data = bytes(data)
            frame = {"type": "frame", "car_id": self.car_id, "seq": self.next_seq,
                     "timestamp_ns": max(timestamp or time.time_ns(), self.last_timestamp_ns + 1),
                     "bus": bus, "can_id": can_id, "is_extended": bool(extended),
                     "is_remote": bool(remote), "is_fd": bool(fd), "is_error": bool(error),
                     "dlc": len(data) if dlc is None else dlc, "data": data.hex().upper()}
            validate_frame(frame)
            self.last_timestamp_ns = frame["timestamp_ns"]
            self.next_seq += 1
            sequences.append(frame["seq"])
            if self.connected:
                self._offer(frame)
            else:
                self.dropped += 1
        return sequences

    def append(self, can_id, data, *, bus="can0", is_extended=False, is_remote=False,
               is_fd=False, is_error=False, dlc=None, timestamp_ns=None):
        return self.append_many([(bus, can_id, data, is_extended, is_remote, is_fd,
                                  is_error, dlc, timestamp_ns)])[0]

    def acknowledge_through(self, seq):
        if not self.last_acked <= seq < self.next_seq:
            raise ValueError("unexpected ACK sequence")
        self.last_acked = seq
        while self.frames and self.frames[0][1]["seq"] <= seq:
            self.frames.popleft()

    def status(self):
        self._trim()
        return {"storage": "ram", "car_id": self.car_id, "queued": len(self.frames),
                "connected": self.connected, "dropped": self.dropped,
                "last_acked": self.last_acked, "next_seq": self.next_seq}


class RawBuffer(FrameBuffer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.streams = OrderedDict()
        self.received = 0

    def last_sequence(self, car_id):
        return self.streams.get(car_id, 0)

    def commit_batch(self, frames):
        """Validate atomically, accept sequence gaps, and ACK RAM acceptance only."""
        if not isinstance(frames, list) or not frames or len(frames) > 4096:
            raise ValueError("invalid frame batch")
        validated = [(frame, validate_frame(frame)) for frame in frames]
        car_id = frames[0]["car_id"]
        if any(frame["car_id"] != car_id for frame in frames):
            raise ValueError("batch contains multiple car identities")
        if any(right["seq"] <= left["seq"] for left, right in zip(frames, frames[1:])):
            raise ValueError("batch sequences must increase")
        last_seq = self.last_sequence(car_id)
        for frame, payload in validated:
            if frame["seq"] <= last_seq:
                continue  # A lost ACK may cause one batch to arrive again.
            self._offer({**frame, "data": payload})
            self.received += 1
        self.streams[car_id] = max(last_seq, frames[-1]["seq"])
        self.streams.move_to_end(car_id)
        if len(self.streams) > 128:
            self.streams.popitem(last=False)
        return frames[-1]["seq"]

    def commit_frame(self, frame):
        return self.commit_batch([frame])

    def finish_export(self, frames):
        exported = {(frame["car_id"], frame["seq"]) for frame in frames}
        self.frames = deque((expiry, frame) for expiry, frame in self.frames
                            if (frame["car_id"], frame["seq"]) not in exported)

    def status(self):
        self._trim()
        return {"storage": "ram", "raw_frames": self.received,
                "pending_influx": len(self.frames), "dropped": self.dropped,
                "streams": dict(self.streams)}

    quick_status = status
