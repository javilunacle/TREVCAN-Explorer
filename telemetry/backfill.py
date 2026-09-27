"""Add decoded summaries to previously exported can_frame points.

This reads the durable raw SQLite archive without changing it. InfluxDB merges
new fields into an existing point with the same measurement, tags, and timestamp.
The default mode is a dry run; --write is required for HTTP writes.
"""

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path

from .server import DBCDecoder, influx_lines, parse_bus_dbcs, post_influx


def backfill(db_path, dbc_paths, influx_url=None, token=None, *, bus_dbc_paths=None,
             write=False, batch_size=250, post=post_influx):
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if write and (not influx_url or not token):
        raise ValueError("InfluxDB URL and token are required for --write")

    decoder = DBCDecoder(dbc_paths, bus_dbc_paths)
    stats = {"scanned": 0, "matched_dbc": 0, "written": 0, "batches": 0}
    batch = []
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        rows = db.execute("""SELECT car_id,seq,timestamp_ns,bus,can_id,is_extended,
            is_remote,is_fd,is_error,dlc,data FROM raw_frames WHERE influx_done=1
            ORDER BY car_id,seq""")
        for row in rows:
            stats["scanned"] += 1
            frame = {"car_id": row[0], "seq": row[1], "timestamp_ns": row[2],
                     "bus": row[3], "can_id": row[4], "is_extended": bool(row[5]),
                     "is_remote": bool(row[6]), "is_fd": bool(row[7]),
                     "is_error": bool(row[8]), "dlc": row[9], "data": row[10]}
            # Only replay the can_frame point, never the can_signal points.
            line = influx_lines([frame], decoder).splitlines()[0]
            if 'message="' not in line:
                continue
            stats["matched_dbc"] += 1
            if write:
                batch.append(line)
                if len(batch) >= batch_size:
                    post(influx_url, token, "\n".join(batch) + "\n")
                    stats["written"] += len(batch)
                    stats["batches"] += 1
                    batch.clear()
        if write and batch:
            post(influx_url, token, "\n".join(batch) + "\n")
            stats["written"] += len(batch)
            stats["batches"] += 1
    finally:
        db.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill decoded frame summaries in InfluxDB")
    parser.add_argument("--db", default="telemetry-server.sqlite3")
    parser.add_argument("--dbc", action="append", default=[],
                        help="DBC file used when these frames were captured; repeat if needed")
    parser.add_argument("--dbc-bus", action="append", default=[], metavar="BUS=PATH",
                        help="DBC restricted to one stored capture bus; repeat as needed")
    parser.add_argument("--influx-url", default=(
        "http://127.0.0.1:8086/api/v2/write?org=docs&bucket=home&precision=ns"))
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--write", action="store_true",
                        help="perform InfluxDB writes; otherwise only count matching frames")
    args = parser.parse_args()
    token = os.getenv("INFLUXDB_TOKEN")
    if args.write and not token:
        token = getpass.getpass("InfluxDB write token: ")
    try:
        bus_dbc_paths = parse_bus_dbcs(args.dbc_bus)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.dbc and not bus_dbc_paths:
        parser.error("at least one --dbc or --dbc-bus mapping is required")
    result = backfill(args.db, args.dbc, args.influx_url, token,
                      bus_dbc_paths=bus_dbc_paths,
                      write=args.write, batch_size=args.batch_size)
    print(json.dumps({"mode": "write" if args.write else "dry-run", **result}, indent=2))


if __name__ == "__main__":
    main()
