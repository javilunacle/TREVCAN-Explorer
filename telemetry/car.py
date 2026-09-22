"""Pi capture, durable SQLite spool, and stop-and-wait TCP sender."""

import argparse
import asyncio
import json
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .common import validate_frame, wire_line


class CarSpool:
    def __init__(self, path, car_id=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS frames (
            seq INTEGER PRIMARY KEY, timestamp_ns INTEGER NOT NULL,
            can_id INTEGER NOT NULL, is_extended INTEGER NOT NULL,
            is_remote INTEGER NOT NULL, is_fd INTEGER NOT NULL, is_error INTEGER NOT NULL,
            dlc INTEGER NOT NULL, data BLOB NOT NULL)""")
        self.db.commit()
        stored = self._meta("car_id")
        if stored and car_id and stored != car_id:
            raise ValueError("car_id differs from existing spool identity")
        self.car_id = stored or car_id or str(uuid.uuid4())
        if not stored:
            with self.db:
                self._set_meta("car_id", self.car_id)
                self._set_meta("next_seq", "1")
                self._set_meta("last_timestamp_ns", "0")
                self._set_meta("last_acked", "0")

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, str(value)))

    def append(self, can_id, data, *, is_extended=False, is_remote=False, is_fd=False,
               is_error=False, dlc=None, timestamp_ns=None):
        data = bytes(data)
        dlc = len(data) if dlc is None else dlc
        if not 0 <= can_id <= (0x1FFFFFFF if is_extended else 0x7FF):
            raise ValueError("invalid CAN ID")
        if len(data) > (64 if is_fd else 8) or not 0 <= dlc <= (64 if is_fd else 8):
            raise ValueError("invalid CAN length")
        if not is_remote and dlc != len(data):
            raise ValueError("DLC does not match data length")
        with self.db:
            seq = int(self._meta("next_seq"))
            timestamp_ns = max(timestamp_ns or time.time_ns(), int(self._meta("last_timestamp_ns")) + 1)
            self.db.execute("INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?)", (
                seq, timestamp_ns, can_id, int(is_extended), int(is_remote), int(is_fd),
                int(is_error), dlc, data
            ))
            self._set_meta("next_seq", seq + 1)
            self._set_meta("last_timestamp_ns", timestamp_ns)
        return seq

    def pending(self, limit=64):
        rows = self.db.execute("SELECT * FROM frames ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [{"type": "frame", "car_id": self.car_id, "seq": seq,
                 "timestamp_ns": timestamp_ns, "can_id": can_id,
                 "is_extended": bool(is_extended), "is_remote": bool(is_remote),
                 "is_fd": bool(is_fd), "is_error": bool(is_error),
                 "dlc": dlc, "data": data.hex().upper()}
                for seq, timestamp_ns, can_id, is_extended, is_remote, is_fd,
                    is_error, dlc, data in rows]

    def oldest(self):
        rows = self.pending(1)
        return rows[0] if rows else None

    def acknowledge(self, seq):
        oldest = self.oldest()
        if oldest is None or oldest["seq"] != seq:
            raise ValueError("ACK does not match oldest unacknowledged frame")
        with self.db:
            self.db.execute("DELETE FROM frames WHERE seq=?", (seq,))
            self._set_meta("last_acked", seq)

    def status(self):
        return {"car_id": self.car_id, "queued": self.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                "last_acked": int(self._meta("last_acked")), "next_seq": int(self._meta("next_seq"))}

    def close(self):
        self.db.close()


async def send_forever(spool, host, port, token=None, stop=None, window_size=64):
    """Leave a row in SQLite until its matching post-commit ACK arrives."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
            try:
                while not stop.is_set():
                    batch = spool.pending(window_size)
                    if not batch:
                        try:
                            await asyncio.wait_for(stop.wait(), 0.1)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    for frame in batch:
                        message = dict(frame)
                        if token:
                            message["token"] = token
                        writer.write(wire_line(message))
                    await writer.drain()
                    for frame in batch:
                        raw = await asyncio.wait_for(reader.readline(), 10)
                        if not raw:
                            raise ConnectionError("receiver disconnected")
                        reply = json.loads(raw)
                        if reply.get("type") == "error":
                            raise ValueError(f"receiver rejected frame: {reply.get('error')}")
                        if reply.get("type") != "ack" or reply.get("seq") != frame["seq"]:
                            raise ValueError("unexpected ACK")
                        spool.acknowledge(frame["seq"])
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            print(f"[sender] disconnected: {exc}; retrying", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except asyncio.TimeoutError:
                pass


def load_simulation_frames(path):
    """Validate a raw-frame replay file before anything is placed in the spool."""
    frames = []
    with open(path, encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
                candidate = {**frame, "type": "frame", "car_id": "simulation",
                             "seq": line_number, "timestamp_ns": 1}
                data = validate_frame(candidate)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid replay frame at line {line_number}: {exc}") from exc
            frames.append((frame["can_id"], data, frame["is_extended"],
                           frame["is_remote"], frame["is_fd"], frame["is_error"],
                           frame["dlc"]))
    if not frames:
        raise ValueError("replay file contains no frames")
    return frames


async def capture_forever(queue, *, interface=None, simulate_rate=None,
                          simulate_file=None, replay_rate=50):
    if simulate_file:
        frames = load_simulation_frames(simulate_file)
        print(f"[simulation] replaying {len(frames)} raw frames at {replay_rate:g} frames/s; "
              "SocketCAN is not used", flush=True)
        index = 0
        while True:
            await queue.put((*frames[index], time.time_ns()))
            index = (index + 1) % len(frames)
            await asyncio.sleep(1 / replay_rate)
    elif simulate_rate:
        index = 0
        while True:
            # master.dbc, ID 160: INV_Temps_1, four little-endian signed 16-bit temperatures.
            temp_tenths = round((25 + 8 * math.sin(index / 20)) * 10)
            data = b"".join(int(temp_tenths + offset).to_bytes(2, "little", signed=True)
                            for offset in (0, 20, 40, 60))
            await queue.put((160, data, False, False, False, False, len(data), time.time_ns()))
            index += 1
            await asyncio.sleep(1 / simulate_rate)
    else:
        try:
            import can
        except ImportError as exc:
            raise RuntimeError("python-can is required for SocketCAN capture") from exc
        with can.Bus(interface="socketcan", channel=interface) as bus:
            while True:
                msg = await asyncio.to_thread(bus.recv, 0.5)
                if msg is not None:
                    # Backpressure is visible here; a full queue may cause kernel CAN loss.
                    await queue.put((msg.arbitration_id, bytes(msg.data), msg.is_extended_id,
                                     msg.is_remote_frame, msg.is_fd, msg.is_error_frame,
                                     msg.dlc, time.time_ns()))


async def spool_writer(spool, queue):
    while True:
        can_id, data, extended, remote, fd, error, dlc, timestamp_ns = await queue.get()
        seq = spool.append(can_id, data, is_extended=extended, is_remote=remote,
                           is_fd=fd, is_error=error, dlc=dlc, timestamp_ns=timestamp_ns)
        queue.task_done()
        if seq % 100 == 0:
            print(f"[spool] {spool.status()}", flush=True)


async def run(args):
    spool = CarSpool(args.db, args.car_id)
    print(f"[car] {spool.status()}", flush=True)
    queue = asyncio.Queue(maxsize=args.queue_size)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(capture_forever(queue, interface=args.interface,
                                              simulate_rate=args.simulate_rate,
                                              simulate_file=args.simulate_file,
                                              replay_rate=args.replay_rate))
            group.create_task(spool_writer(spool, queue))
            group.create_task(send_forever(spool, args.host, args.port, args.token,
                                           window_size=args.window_size))
    finally:
        spool.close()


def main():
    parser = argparse.ArgumentParser(description="Pi raw-CAN capture and durable sender")
    parser.add_argument("--db", default="telemetry-car.sqlite3")
    parser.add_argument("--host", default="127.0.0.1", help="laptop receiver IP")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("TELEMETRY_TOKEN"),
                        help="shared demo token (or TELEMETRY_TOKEN environment variable)")
    parser.add_argument("--car-id", help="stable identity; saved on first start")
    parser.add_argument("--status", action="store_true", help="print local spool status and exit")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--interface", help="SocketCAN interface, e.g. can0")
    source.add_argument("--simulate-rate", type=float, help="synthetic frames per second")
    source.add_argument("--simulate-file", help="loop a raw JSONL replay file; no CAN bus transmission")
    parser.add_argument("--replay-rate", type=float, default=50,
                        help="raw replay frames per second with --simulate-file (default: 50)")
    parser.add_argument("--queue-size", type=int, default=1000)
    parser.add_argument("--window-size", type=int, default=64,
                        help="maximum unacknowledged frames in one TCP batch")
    args = parser.parse_args()
    if args.status:
        spool = CarSpool(args.db, args.car_id)
        try:
            print(json.dumps(spool.status(), indent=2))
        finally:
            spool.close()
        return
    if args.interface is None and args.simulate_rate is None and args.simulate_file is None:
        parser.error("provide --interface, --simulate-rate, or --simulate-file")
    if (args.queue_size < 1 or args.window_size < 1 or
            args.replay_rate <= 0 or
            (args.simulate_rate is not None and args.simulate_rate <= 0)):
        parser.error("queue size, window size, and simulation rates must be positive")
    if args.simulate_file:
        try:
            load_simulation_frames(args.simulate_file)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
