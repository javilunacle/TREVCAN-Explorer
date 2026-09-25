"""Pi multi-bus capture, batched durable SQLite spool, and TCP sender."""

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


class SpoolLock:
    """Prevent two sender processes from mutating the same sequence spool."""

    def __init__(self, path):
        self.path = f"{path}.lock"
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.file.seek(0, os.SEEK_END) == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.file.close()
            raise RuntimeError(f"spool is already in use: {path}") from exc

    def close(self):
        if self.file.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class CarSpool:
    def __init__(self, path, car_id=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = SpoolLock(path)
        try:
            self.db = sqlite3.connect(path)
        except BaseException:
            self.lock.close()
            raise
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS frames (
            seq INTEGER PRIMARY KEY, timestamp_ns INTEGER NOT NULL,
            can_id INTEGER NOT NULL, is_extended INTEGER NOT NULL,
            is_remote INTEGER NOT NULL, is_fd INTEGER NOT NULL, is_error INTEGER NOT NULL,
            dlc INTEGER NOT NULL, data BLOB NOT NULL,
            bus TEXT NOT NULL DEFAULT 'can0')""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(frames)")}
        if "bus" not in columns:
            self.db.execute("ALTER TABLE frames ADD COLUMN bus TEXT NOT NULL DEFAULT 'can0'")
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

    def append_many(self, records):
        """Durably append a capture batch in one SQLite transaction."""
        if not records:
            return []
        rows = []
        with self.db:
            next_seq = int(self._meta("next_seq"))
            last_timestamp_ns = int(self._meta("last_timestamp_ns"))
            for record in records:
                (bus, can_id, data, is_extended, is_remote, is_fd, is_error,
                 dlc, timestamp_ns) = record
                data = bytes(data)
                dlc = len(data) if dlc is None else dlc
                candidate = {"type": "frame", "car_id": self.car_id, "seq": next_seq,
                             "timestamp_ns": max(timestamp_ns or time.time_ns(), last_timestamp_ns + 1),
                             "bus": bus, "can_id": can_id,
                             "is_extended": bool(is_extended), "is_remote": bool(is_remote),
                             "is_fd": bool(is_fd), "is_error": bool(is_error),
                             "dlc": dlc, "data": data.hex()}
                validate_frame(candidate)
                last_timestamp_ns = candidate["timestamp_ns"]
                rows.append((next_seq, last_timestamp_ns, can_id, int(is_extended),
                             int(is_remote), int(is_fd), int(is_error), dlc, data, bus))
                next_seq += 1
            self.db.executemany("""INSERT INTO frames
                (seq,timestamp_ns,can_id,is_extended,is_remote,is_fd,is_error,dlc,data,bus)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
            self._set_meta("next_seq", next_seq)
            self._set_meta("last_timestamp_ns", last_timestamp_ns)
        return [row[0] for row in rows]

    def append(self, can_id, data, *, bus="can0", is_extended=False, is_remote=False,
               is_fd=False, is_error=False, dlc=None, timestamp_ns=None):
        return self.append_many([(bus, can_id, data, is_extended, is_remote, is_fd,
                                  is_error, dlc, timestamp_ns)])[0]

    def pending(self, limit=64):
        rows = self.db.execute("""SELECT seq,timestamp_ns,can_id,is_extended,is_remote,
            is_fd,is_error,dlc,data,bus FROM frames ORDER BY seq LIMIT ?""", (limit,)).fetchall()
        return [{"type": "frame", "car_id": self.car_id, "seq": seq, "bus": bus,
                 "timestamp_ns": timestamp_ns, "can_id": can_id,
                 "is_extended": bool(is_extended), "is_remote": bool(is_remote),
                 "is_fd": bool(is_fd), "is_error": bool(is_error),
                 "dlc": dlc, "data": data.hex().upper()}
                for seq, timestamp_ns, can_id, is_extended, is_remote, is_fd,
                    is_error, dlc, data, bus in rows]

    def oldest(self):
        rows = self.pending(1)
        return rows[0] if rows else None

    def acknowledge_through(self, seq):
        bounds = self.db.execute("SELECT MIN(seq),MAX(seq) FROM frames").fetchone()
        if bounds[0] is None:
            if seq <= int(self._meta("last_acked")):
                return
            raise ValueError("ACK exceeds the local spool")
        if not bounds[0] <= seq <= bounds[1]:
            raise ValueError("ACK is outside the pending spool range")
        with self.db:
            self.db.execute("DELETE FROM frames WHERE seq<=?", (seq,))
            self._set_meta("last_acked", seq)

    def acknowledge(self, seq):
        oldest = self.oldest()
        if oldest is None or oldest["seq"] != seq:
            raise ValueError("ACK does not match oldest unacknowledged frame")
        self.acknowledge_through(seq)

    def status(self):
        return {"car_id": self.car_id, "queued": self.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                "last_acked": int(self._meta("last_acked")), "next_seq": int(self._meta("next_seq"))}

    def close(self):
        try:
            self.db.close()
        finally:
            self.lock.close()


def read_spool_status(path):
    """Read live spool counters without taking the single-writer lock."""
    resolved = Path(path).resolve().as_posix()
    db = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        meta = dict(db.execute("SELECT key,value FROM meta"))
        columns = {row[1] for row in db.execute("PRAGMA table_info(frames)")}
        buses = (dict(db.execute("SELECT bus,COUNT(*) FROM frames GROUP BY bus"))
                 if "bus" in columns else {"legacy": db.execute(
                     "SELECT COUNT(*) FROM frames").fetchone()[0]})
        return {"car_id": meta["car_id"],
                "queued": sum(buses.values()),
                "queued_by_bus": buses,
                "last_acked": int(meta["last_acked"]),
                "next_seq": int(meta["next_seq"])}
    finally:
        db.close()


async def send_forever(spool, host, port, token=None, stop=None, window_size=64):
    """Retain rows until a cumulative post-commit ACK covers them."""
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
                    message = {"type": "batch", "frames": batch}
                    if token:
                        message["token"] = token
                    writer.write(wire_line(message))
                    await writer.drain()
                    raw = await asyncio.wait_for(reader.readline(), 10)
                    if not raw:
                        raise ConnectionError("receiver disconnected")
                    reply = json.loads(raw)
                    if reply.get("type") == "error":
                        raise ValueError(f"receiver rejected batch: {reply.get('error')}")
                    if reply.get("type") != "ack" or reply.get("seq") != batch[-1]["seq"]:
                        raise ValueError("unexpected cumulative ACK")
                    spool.acknowledge_through(reply["seq"])
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
            await queue.put(("simulation", *frames[index], time.time_ns()))
            index = (index + 1) % len(frames)
            await asyncio.sleep(1 / replay_rate)
    elif simulate_rate:
        index = 0
        while True:
            # master.dbc, ID 160: INV_Temps_1, four little-endian signed 16-bit temperatures.
            temp_tenths = round((25 + 8 * math.sin(index / 20)) * 10)
            data = b"".join(int(temp_tenths + offset).to_bytes(2, "little", signed=True)
                            for offset in (0, 20, 40, 60))
            await queue.put(("simulation", 160, data, False, False, False, False,
                             len(data), time.time_ns()))
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
                    await queue.put((interface, msg.arbitration_id, bytes(msg.data), msg.is_extended_id,
                                     msg.is_remote_frame, msg.is_fd, msg.is_error_frame,
                                     msg.dlc, time.time_ns()))


async def spool_writer(spool, queue, batch_size=256, flush_ms=20):
    while True:
        records = [await queue.get()]
        deadline = asyncio.get_running_loop().time() + flush_ms / 1000
        while len(records) < batch_size:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                records.append(await asyncio.wait_for(queue.get(), remaining))
            except asyncio.TimeoutError:
                break
        sequences = spool.append_many(records)
        for _ in records:
            queue.task_done()
        if sequences[-1] // 100 != (sequences[0] - 1) // 100:
            print(f"[spool] {spool.status()}", flush=True)


async def run(args):
    spool = CarSpool(args.db, args.car_id)
    print(f"[car] {spool.status()}", flush=True)
    queue = asyncio.Queue(maxsize=args.queue_size)
    try:
        async with asyncio.TaskGroup() as group:
            interfaces = args.interface or [None]
            for interface in interfaces:
                group.create_task(capture_forever(queue, interface=interface,
                                                  simulate_rate=args.simulate_rate,
                                                  simulate_file=args.simulate_file,
                                                  replay_rate=args.replay_rate))
            group.create_task(spool_writer(spool, queue, args.spool_batch_size,
                                           args.spool_flush_ms))
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
    source.add_argument("--interface", action="append",
                        help="SocketCAN interface; repeat for can0 and can1")
    source.add_argument("--simulate-rate", type=float, help="synthetic frames per second")
    source.add_argument("--simulate-file", help="loop a raw JSONL replay file; no CAN bus transmission")
    parser.add_argument("--replay-rate", type=float, default=50,
                        help="raw replay frames per second with --simulate-file (default: 50)")
    parser.add_argument("--queue-size", type=int, default=1000)
    parser.add_argument("--window-size", type=int, default=256,
                        help="maximum frames in one TCP transaction (default: 256)")
    parser.add_argument("--spool-batch-size", type=int, default=256,
                        help="maximum frames in one car SQLite transaction")
    parser.add_argument("--spool-flush-ms", type=float, default=20,
                        help="maximum capture time held before a spool commit")
    args = parser.parse_args()
    if args.status:
        try:
            print(json.dumps(read_spool_status(args.db), indent=2))
        except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
            parser.error(f"cannot read spool status: {exc}")
        return
    if args.interface is None and args.simulate_rate is None and args.simulate_file is None:
        parser.error("provide --interface, --simulate-rate, or --simulate-file")
    if (args.queue_size < 1 or args.window_size < 1 or args.spool_batch_size < 1 or
            args.spool_flush_ms <= 0 or
            args.replay_rate <= 0 or
            (args.simulate_rate is not None and args.simulate_rate <= 0)):
        parser.error("queue, batch, window, flush, and simulation values must be positive")
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
