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
            timestamp_ns INTEGER NOT NULL, can_id INTEGER NOT NULL,
            is_extended INTEGER NOT NULL, is_remote INTEGER NOT NULL,
            is_fd INTEGER NOT NULL, is_error INTEGER NOT NULL,
            dlc INTEGER NOT NULL, data BLOB NOT NULL,
            influx_done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (car_id, seq))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS streams (
            car_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS raw_pending ON raw_frames(influx_done, car_id, seq)")
        self.db.commit()

    def commit_frame(self, frame):
        payload = validate_frame(frame)
        car_id, seq = frame["car_id"], frame["seq"]
        try:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT timestamp_ns,can_id,is_extended,is_remote,is_fd,is_error,dlc,data "
                "FROM raw_frames WHERE car_id=? AND seq=?", (car_id, seq)
            ).fetchone()
            values = (frame["timestamp_ns"], frame["can_id"], int(frame["is_extended"]),
                      int(frame["is_remote"]), int(frame["is_fd"]), int(frame["is_error"]),
                      frame["dlc"], payload)
            if previous is not None:
                if previous != values:
                    raise ValueError("sequence collision with different frame")
            else:
                state = self.db.execute("SELECT last_seq FROM streams WHERE car_id=?", (car_id,)).fetchone()
                last_seq = state[0] if state else 0
                if seq != last_seq + 1:
                    raise ValueError(f"sequence gap: expected {last_seq + 1}, got {seq}")
                self.db.execute("""INSERT INTO raw_frames
                    (car_id,seq,timestamp_ns,can_id,is_extended,is_remote,is_fd,is_error,dlc,data)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (car_id, seq, *values))
                self.db.execute("INSERT OR REPLACE INTO streams(car_id,last_seq) VALUES (?,?)", (car_id, seq))
            self.db.commit()  # ACK must be sent only after this durable commit.
            return seq
        except BaseException:
            self.db.rollback()
            raise

    def pending(self, limit=500):
        rows = self.db.execute("""SELECT car_id,seq,timestamp_ns,can_id,is_extended,
            is_remote,is_fd,is_error,dlc,data FROM raw_frames WHERE influx_done=0
            ORDER BY car_id,seq LIMIT ?""", (limit,)).fetchall()
        return [{"car_id": r[0], "seq": r[1], "timestamp_ns": r[2], "can_id": r[3],
                 "is_extended": bool(r[4]), "is_remote": bool(r[5]), "is_fd": bool(r[6]),
                 "is_error": bool(r[7]), "dlc": r[8], "data": r[9]} for r in rows]

    def mark_exported(self, frames):
        with self.db:
            self.db.executemany("UPDATE raw_frames SET influx_done=1 WHERE car_id=? AND seq=?",
                                [(f["car_id"], f["seq"]) for f in frames])

    def status(self):
        total, pending = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(1-influx_done),0) FROM raw_frames").fetchone()
        streams = dict(self.db.execute("SELECT car_id,last_seq FROM streams").fetchall())
        return {"raw_frames": total, "pending_influx": pending, "streams": streams}

    def close(self):
        self.db.close()


class DBCDecoder:
    def __init__(self, paths):
        if paths:
            import cantools
            self.databases = [(Path(path).name, cantools.database.load_file(path, strict=False))
                              for path in paths]
        else:
            self.databases = []

    def decode(self, frame):
        if frame["is_error"] or frame["is_remote"]:
            return None
        for filename, database in self.databases:
            for message in database.messages:
                if (message.frame_id == frame["can_id"] and
                        bool(message.is_extended_frame) == frame["is_extended"]):
                    try:
                        return filename, message.name, message.decode(frame["data"])
                    except Exception as exc:
                        print(f"[decode] {filename} seq={frame['seq']}: {exc}", flush=True)
                        return None
        return None


def _escape_tag(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def influx_lines(frames, decoder):
    lines = []
    for frame in frames:
        base = f"car={_escape_tag(frame['car_id'])},can_id=0x{frame['can_id']:X},ext={str(frame['is_extended']).lower()}"
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
            tags = (f"car={_escape_tag(frame['car_id'])},can_id=0x{frame['can_id']:X},"
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


async def export_forever(store, decoder, url, token, stop=None):
    stop = stop or asyncio.Event()
    while not stop.is_set():
        batch = store.pending()
        if not batch:
            try:
                await asyncio.wait_for(stop.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            payload = influx_lines(batch, decoder)
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
                frame = json.loads(raw)
                if token and frame.get("token") != token:
                    raise ValueError("invalid token")
                seq = store.commit_frame(frame)
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
            args.host, args.port, limit=4096)
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
