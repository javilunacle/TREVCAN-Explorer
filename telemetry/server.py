"""Laptop TCP receiver: commit raw frames, ACK, then decode/export to InfluxDB."""

import argparse
import asyncio
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from .common import validate_frame, wire_line


class RawStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS raw_frames (
            car_id TEXT NOT NULL, seq INTEGER NOT NULL,
            timestamp_ns INTEGER NOT NULL, bus TEXT NOT NULL DEFAULT 'unknown', can_id INTEGER NOT NULL,
            is_extended INTEGER NOT NULL, is_remote INTEGER NOT NULL,
            is_fd INTEGER NOT NULL, is_error INTEGER NOT NULL,
            dlc INTEGER NOT NULL, data BLOB NOT NULL,
            influx_done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (car_id, seq))""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(raw_frames)")}
        if "bus" not in columns:
            self.db.execute("ALTER TABLE raw_frames ADD COLUMN bus TEXT NOT NULL DEFAULT 'unknown'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS streams (
            car_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS raw_pending ON raw_frames(influx_done, car_id, seq)")
        self.db.commit()

    def commit_batch(self, frames):
        if not isinstance(frames, list) or not frames or len(frames) > 4096:
            raise ValueError("invalid frame batch")
        validated = [(frame, validate_frame(frame)) for frame in frames]
        car_id = frames[0]["car_id"]
        if any(frame["car_id"] != car_id for frame in frames):
            raise ValueError("batch contains multiple car identities")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.db.execute(
                "SELECT last_seq FROM streams WHERE car_id=?", (car_id,)).fetchone()
            last_seq = state[0] if state else 0
            for frame, payload in validated:
                seq = frame["seq"]
                values = (frame["timestamp_ns"], frame.get("bus", "unknown"), frame["can_id"],
                          int(frame["is_extended"]), int(frame["is_remote"]),
                          int(frame["is_fd"]), int(frame["is_error"]), frame["dlc"], payload)
                if seq <= last_seq:
                    previous = self.db.execute(
                        "SELECT timestamp_ns,bus,can_id,is_extended,is_remote,is_fd,is_error,dlc,data "
                        "FROM raw_frames WHERE car_id=? AND seq=?", (car_id, seq)).fetchone()
                    if (previous is not None and previous[1] == "unknown" and
                            previous[:1] + previous[2:] == values[:1] + values[2:]):
                        self.db.execute(
                            "UPDATE raw_frames SET bus=? WHERE car_id=? AND seq=?",
                            (values[1], car_id, seq))
                    elif previous != values:
                        raise ValueError("sequence collision with different frame")
                    continue
                if seq != last_seq + 1:
                    raise ValueError(f"sequence gap: expected {last_seq + 1}, got {seq}")
                self.db.execute("""INSERT INTO raw_frames
                    (car_id,seq,timestamp_ns,bus,can_id,is_extended,is_remote,is_fd,is_error,dlc,data)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (car_id, seq, *values))
                last_seq = seq
            self.db.execute("INSERT OR REPLACE INTO streams(car_id,last_seq) VALUES (?,?)",
                            (car_id, last_seq))
            self.db.commit()  # ACK must be sent only after this durable commit.
            return frames[-1]["seq"]
        except BaseException:
            self.db.rollback()
            raise

    def commit_frame(self, frame):
        return self.commit_batch([frame])

    def pending(self, limit=5000):
        rows = self.db.execute("""SELECT car_id,seq,timestamp_ns,bus,can_id,is_extended,
            is_remote,is_fd,is_error,dlc,data FROM raw_frames WHERE influx_done=0
            ORDER BY car_id,seq LIMIT ?""", (limit,)).fetchall()
        return [{"car_id": r[0], "seq": r[1], "timestamp_ns": r[2], "bus": r[3],
                 "can_id": r[4], "is_extended": bool(r[5]), "is_remote": bool(r[6]),
                 "is_fd": bool(r[7]), "is_error": bool(r[8]), "dlc": r[9],
                 "data": r[10]} for r in rows]

    def mark_exported(self, frames):
        with self.db:
            ranges = {}
            for frame in frames:
                bounds = ranges.setdefault(frame["car_id"], [frame["seq"], frame["seq"]])
                bounds[0] = min(bounds[0], frame["seq"])
                bounds[1] = max(bounds[1], frame["seq"])
            self.db.executemany(
                "UPDATE raw_frames SET influx_done=1 WHERE car_id=? AND seq BETWEEN ? AND ?",
                [(car_id, bounds[0], bounds[1]) for car_id, bounds in ranges.items()])

    def status(self):
        total, pending = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(1-influx_done),0) FROM raw_frames").fetchone()
        streams = dict(self.db.execute("SELECT car_id,last_seq FROM streams").fetchall())
        return {"raw_frames": total, "pending_influx": pending, "streams": streams}

    def close(self):
        self.db.close()


class DBCDecoder:
    def __init__(self, paths):
        self.messages = {}
        self.failure_count = 0
        if paths:
            import cantools
            self.databases = [(Path(path).name, cantools.database.load_file(path, strict=False))
                              for path in paths]
        else:
            self.databases = []
        for filename, database in self.databases:
            for message in database.messages:
                key = (message.frame_id, bool(message.is_extended_frame))
                self.messages.setdefault(key, []).append((filename, message))

    def decode(self, frame):
        if frame["is_error"] or frame["is_remote"]:
            return None
        failures = []
        for filename, message in self.messages.get(
                (frame["can_id"], frame["is_extended"]), []):
            try:
                return filename, message.name, message.decode(frame["data"])
            except Exception as exc:
                failures.append(f"{filename}: {exc}")
        if failures:
            self.failure_count += 1
            if self.failure_count <= 5 or self.failure_count % 1000 == 0:
                print(f"[decode] failures={self.failure_count} bus={frame.get('bus', 'unknown')} "
                      f"seq={frame['seq']}: {'; '.join(failures)}", flush=True)
        return None


def _escape_tag(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def influx_lines(frames, decoder):
    lines = []
    for frame in frames:
        base = (f"car={_escape_tag(frame['car_id'])},bus={_escape_tag(frame.get('bus', 'unknown'))},"
                f"can_id=0x{frame['can_id']:X},ext={str(frame['is_extended']).lower()}")
        data = frame["data"]
        decoded = decoder.decode(frame)
        fields = [f"seq={frame['seq']}i", f"dlc={frame['dlc']}i",
                  f'data="{data.hex().upper()}"',
                  f"remote={str(frame['is_remote']).lower()}",
                  f"fd={str(frame['is_fd']).lower()}",
                  f"error={str(frame['is_error']).lower()}"]
        fields.extend(f"b{i}={byte}i" for i, byte in enumerate(data))
        if decoded is not None:
            dbc_file, message_name, signals = decoded
            summary = "; ".join(
                f"{name}={value.name} ({value.value})"
                if hasattr(value, "name") and hasattr(value, "value") else f"{name}={value}"
                for name, value in signals.items()
            )
            fields.extend((f'message="{_escape_field(message_name)}"',
                           f'dbc="{_escape_field(dbc_file)}"',
                           f'decoded="{_escape_field(summary)}"'))
        lines.append(f"can_frame,{base} {','.join(fields)} {frame['timestamp_ns']}")
        if decoded is None:
            continue
        dbc_file, message_name, signals = decoded
        for name, value in signals.items():
            tags = (f"car={_escape_tag(frame['car_id'])},bus={_escape_tag(frame.get('bus', 'unknown'))},"
                    f"can_id=0x{frame['can_id']:X},"
                    f"message={_escape_tag(message_name)},signal={_escape_tag(name)},"
                    f"dbc={_escape_tag(dbc_file)}")
            signal_fields = [f"seq={frame['seq']}i"]
            if hasattr(value, "name") and hasattr(value, "value"):
                signal_fields.append(f"value={float(value.value)}")
                signal_fields.append(f'state="{_escape_field(value.name)}"')
            elif isinstance(value, (int, float)):
                signal_fields.append(f"value={float(value)}")
            else:
                continue
            lines.append(f"can_signal,{tags} {','.join(signal_fields)} {frame['timestamp_ns']}")
    return "\n".join(lines) + ("\n" if lines else "")


def influx_snapshot_lines(frames, decoder):
    """Decode all inputs but export only current raw/message and signal values."""
    latest_frames = {}
    latest_signals = {}
    for frame in frames:
        decoded = decoder.decode(frame)
        frame_key = (frame["car_id"], frame.get("bus", "unknown"),
                     frame["can_id"], frame["is_extended"])
        latest_frames[frame_key] = (frame, decoded)
        if decoded is None:
            continue
        dbc_file, message_name, signals = decoded
        for name, value in signals.items():
            latest_signals[(frame["car_id"], frame.get("bus", "unknown"),
                            frame["can_id"], message_name, name, dbc_file)] = (frame, value)

    lines = []
    for frame, decoded in latest_frames.values():
        base = (f"car={_escape_tag(frame['car_id'])},bus={_escape_tag(frame.get('bus', 'unknown'))},"
                f"can_id=0x{frame['can_id']:X},ext={str(frame['is_extended']).lower()}")
        data = frame["data"]
        fields = [f"seq={frame['seq']}i", f"dlc={frame['dlc']}i",
                  f'data="{data.hex().upper()}"',
                  f"remote={str(frame['is_remote']).lower()}",
                  f"fd={str(frame['is_fd']).lower()}",
                  f"error={str(frame['is_error']).lower()}"]
        fields.extend(f"b{i}={byte}i" for i, byte in enumerate(data))
        if decoded is not None:
            dbc_file, message_name, signals = decoded
            summary = "; ".join(
                f"{name}={value.name} ({value.value})"
                if hasattr(value, "name") and hasattr(value, "value") else f"{name}={value}"
                for name, value in signals.items())
            fields.extend((f'message="{_escape_field(message_name)}"',
                           f'dbc="{_escape_field(dbc_file)}"',
                           f'decoded="{_escape_field(summary)}"'))
        lines.append(f"can_frame,{base} {','.join(fields)} {frame['timestamp_ns']}")

    for key, (frame, value) in latest_signals.items():
        car_id, bus, can_id, message_name, name, dbc_file = key
        tags = (f"car={_escape_tag(car_id)},bus={_escape_tag(bus)},can_id=0x{can_id:X},"
                f"message={_escape_tag(message_name)},signal={_escape_tag(name)},"
                f"dbc={_escape_tag(dbc_file)}")
        signal_fields = [f"seq={frame['seq']}i"]
        if hasattr(value, "name") and hasattr(value, "value"):
            signal_fields.append(f"value={float(value.value)}")
            signal_fields.append(f'state="{_escape_field(value.name)}"')
        elif isinstance(value, (int, float)):
            signal_fields.append(f"value={float(value)}")
        else:
            continue
        lines.append(f"can_signal,{tags} {','.join(signal_fields)} {frame['timestamp_ns']}")
    return "\n".join(lines) + ("\n" if lines else "")


def post_influx(url, token, payload):
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Token {token}"
    request = urllib.request.Request(url, data=payload.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"InfluxDB returned HTTP {response.status}")


async def export_forever(store, decoder, url, token, stop=None, *, batch_size=5000,
                         interval_seconds=0.1):
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), interval_seconds)
            continue
        except asyncio.TimeoutError:
            pass
        batch = store.pending(batch_size)
        if not batch:
            continue
        try:
            payload = await asyncio.to_thread(influx_snapshot_lines, batch, decoder)
            await asyncio.to_thread(post_influx, url, token, payload)
            store.mark_exported(batch)
        except (OSError, urllib.error.HTTPError, RuntimeError) as exc:
            print(f"[influx] export failed: {exc}; raw frames retained", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass


async def serve_client(reader, writer, store, token):
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                message = json.loads(raw)
                if token and message.get("token") != token:
                    raise ValueError("invalid token")
                if message.get("type") == "batch":
                    seq = store.commit_batch(message.get("frames"))
                else:
                    seq = store.commit_frame(message)
                writer.write(wire_line({"type": "ack", "seq": seq}))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                writer.write(wire_line({"type": "error", "error": str(exc)}))
                await writer.drain()
                break
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def run(args):
    store = RawStore(args.db)
    decoder = DBCDecoder(args.dbc)
    print(f"[server] {store.status()}", flush=True)
    try:
        server = await asyncio.start_server(
            lambda reader, writer: serve_client(reader, writer, store, args.token),
            args.host, args.port, limit=1024 * 1024)
        print(f"[server] listening on {args.host}:{args.port}", flush=True)
        async with server:
            if args.influx_url:
                async with asyncio.TaskGroup() as group:
                    group.create_task(server.serve_forever())
                    group.create_task(export_forever(store, decoder, args.influx_url, args.influx_token))
            else:
                await server.serve_forever()
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Laptop durable raw-CAN receiver and downstream decoder")
    parser.add_argument("--db", default="telemetry-server.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("TELEMETRY_TOKEN"),
                        help="shared demo token (or TELEMETRY_TOKEN environment variable)")
    parser.add_argument("--dbc", action="append", default=[], help="DBC path; repeat for priority order")
    parser.add_argument("--influx-url", help="full InfluxDB v2 write URL with precision=ns")
    parser.add_argument("--influx-token", default=os.getenv("INFLUXDB_TOKEN"))
    parser.add_argument("--status", action="store_true", help="print raw store counts and exit")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token and not args.status:
        parser.error("a TELEMETRY_TOKEN is required when listening beyond localhost")
    if args.status:
        store = RawStore(args.db)
        try:
            print(json.dumps(store.status(), indent=2))
        finally:
            store.close()
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
