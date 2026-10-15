"""RAM-batched CAN receiver with independent Live and InfluxDB publishing."""

import argparse
import asyncio
import copy
import json
import math
import os
import urllib.request
from pathlib import Path

from .common import wire_line
from .buffers import RawBuffer
from .live import GrafanaLivePublisher, stream_namespace
from .transforms import TransformEngine, TransformPlan




class DBCDecoder:
    def __init__(self, paths=(), bus_paths=None):
        """Load global DBCs plus optional DBCs restricted to one capture bus.

        A bus with an explicit mapping never falls back to the global map. This
        prevents an overlapping CAN ID on can0 from being decoded with a can1
        definition merely because that DBC appeared first on the command line.
        """
        self.messages = {}
        self.bus_messages = {}
        self.failure_count = 0
        self.databases = []
        if not paths and not bus_paths:
            return
        import cantools

        loaded = {}

        def load(path):
            resolved = str(Path(path).resolve())
            if resolved not in loaded:
                loaded[resolved] = (
                    Path(path).name,
                    cantools.database.load_file(path, strict=False),
                )
            return loaded[resolved]

        self.databases = [load(path) for path in paths]
        for filename, database in self.databases:
            for message in database.messages:
                key = (message.frame_id, bool(message.is_extended_frame))
                self.messages.setdefault(key, []).append((filename, message))

        for bus, configured_paths in (bus_paths or {}).items():
            messages = self.bus_messages.setdefault(bus, {})
            for filename, database in (load(path) for path in configured_paths):
                for message in database.messages:
                    key = (message.frame_id, bool(message.is_extended_frame))
                    messages.setdefault(key, []).append((filename, message))

    def decode(self, frame):
        if frame["is_error"] or frame["is_remote"]:
            return None
        next_failure_count = self.failure_count + 1
        should_log_failure = next_failure_count <= 5 or next_failure_count % 1000 == 0
        had_failure = False
        failures = []
        bus = frame.get("bus", "unknown")
        messages = self.bus_messages.get(bus, self.messages)
        for filename, message in messages.get(
                (frame["can_id"], frame["is_extended"]), []):
            if len(frame["data"]) != message.length:
                had_failure = True
                if should_log_failure:
                    failures.append(
                        f"{filename}:{message.name}[expected_dlc={message.length}]: "
                        f"Wrong data size: {len(frame['data'])} instead of "
                        f"{message.length} bytes")
                continue
            try:
                return filename, message.name, message.decode(frame["data"])
            except Exception as exc:
                had_failure = True
                if should_log_failure:
                    failures.append(
                        f"{filename}:{message.name}[expected_dlc={message.length}]: {exc}")
        if had_failure:
            self.failure_count = next_failure_count
            if should_log_failure:
                print(f"[decode] failures={self.failure_count} bus={bus} "
                      f"can_id=0x{frame['can_id']:X} dlc={frame['dlc']} "
                      f"seq={frame['seq']}: {'; '.join(failures)}", flush=True)
        return None


def parse_bus_dbcs(values):
    """Convert repeated BUS=PATH arguments into an ordered bus-to-DBC map."""
    result = {}
    for value in values:
        bus, separator, path = value.partition("=")
        if not separator or not bus or not path:
            raise ValueError(f"invalid bus DBC mapping {value!r}; expected BUS=PATH")
        if any(character not in
               "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
               for character in bus):
            raise ValueError(f"invalid bus name in DBC mapping: {bus!r}")
        result.setdefault(bus, []).append(path)
    return result


def _escape_tag(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


_TABLE_ROW_SEPARATOR = "\u241f"


def _table_row_value(*values):
    """Pack display-only columns without creating six Flux field streams."""
    return _TABLE_ROW_SEPARATOR.join(
        str(value).replace(_TABLE_ROW_SEPARATOR, " ").replace("\r", " ").replace("\n", " ")
        for value in values
    )




def prepare_influx_history(frames, decoder, transformer=None):
    """Preserve each source sample and stage retry-safe derived transform state."""
    decoded_frames = []
    signal_samples = []
    for frame in frames:
        decoded = decoder.decode(frame)
        decoded_frames.append((frame, decoded))
        if decoded is None:
            continue
        dbc_file, message_name, signals = decoded
        for name, value in signals.items():
            key = (frame["car_id"], frame.get("bus", "unknown"),
                   frame["can_id"], message_name, name, dbc_file)
            signal_samples.append((key, (frame, value)))

    lines = []
    for frame, decoded in decoded_frames:
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
        else:
            message_name = "Unknown"
            dbc_file = "Unknown / undecoded"
            summary = ""
            fields.extend((f'message="{message_name}"', f'dbc="{dbc_file}"',
                           'decoded=""'))
        table_row = _table_row_value(
            frame["seq"], frame["dlc"], data.hex().upper(), dbc_file,
            message_name, summary,
        )
        fields.append(f'table_row="{_escape_field(table_row)}"')
        lines.append(f"can_frame,{base} {','.join(fields)} {frame['timestamp_ns']}")

    for key, (frame, value) in signal_samples:
        car_id, bus, can_id, message_name, name, dbc_file = key
        tags = (f"car={_escape_tag(car_id)},bus={_escape_tag(bus)},can_id=0x{can_id:X},"
                f"message={_escape_tag(message_name)},signal={_escape_tag(name)},"
                f"dbc={_escape_tag(dbc_file)}")
        signal_fields = [f"seq={frame['seq']}i"]
        if hasattr(value, "name") and hasattr(value, "value"):
            signal_fields.append(f"value={float(value.value)}")
            signal_fields.append(f'state="{_escape_field(value.name)}"')
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue  # Influx cannot store NaN/Inf; raw bytes remain available.
            signal_fields.append(f"value={float(value)}")
        else:
            continue
        lines.append(f"can_signal,{tags} {','.join(signal_fields)} {frame['timestamp_ns']}")
    payload = "\n".join(lines) + ("\n" if lines else "")
    plan = transformer.prepare(decoded_frames) if transformer else TransformPlan([], {})
    return payload + plan.line_protocol(), plan




def post_influx(url, token, payload):
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Token {token}"
    request = urllib.request.Request(url, data=payload.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"InfluxDB returned HTTP {response.status}")


async def export_forever(store, decoder, url, token, stop=None, *, batch_size=5000,
                         interval_seconds=0.1, transformer=None):
    stop = stop or asyncio.Event()
    last_status = asyncio.get_running_loop().time()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), interval_seconds)
            continue
        except asyncio.TimeoutError:
            pass
        now = asyncio.get_running_loop().time()
        if now - last_status >= 5:
            print(f"[history-buffer] {store.status()}", flush=True)
            last_status = now
        batch = store.pending(batch_size)
        if not batch:
            continue
        try:
            try:
                payload, plan = await asyncio.to_thread(
                    prepare_influx_history, batch, decoder, transformer)
            except Exception as exc:
                # Raw and ordinary decoded telemetry remain available even if a
                # transform policy has a bug or receives an unexpected value.
                print(f"[transform] batch skipped: {exc}", flush=True)
                payload, plan = await asyncio.to_thread(
                    prepare_influx_history, batch, decoder, None)
            await asyncio.to_thread(post_influx, url, token, payload)
            store.finish_export(batch)
            if transformer is not None:
                transformer.commit(plan)
        except Exception as exc:
            store.discard_pending()
            print(f"[influx] export failed: {exc}; disconnected data discarded", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass
            store.discard_pending()  # Resume with samples arriving after the retry pause.




async def serve_client(reader, writer, store, token, live=None):
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                message = json.loads(raw)
                if token and message.get("token") != token:
                    raise ValueError("invalid token")
                frames = (message.get("frames") if message.get("type") == "batch"
                          else [message])
                # Validate/accept before offering anything to the best-effort live
                # branch. Retransmissions after a lost ACK must not stream twice.
                previous = (store.last_sequence(frames[0].get("car_id"))
                            if live and isinstance(frames, list) and frames
                            and isinstance(frames[0], dict)
                            and isinstance(frames[0].get("car_id"), str) else 0)
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
            if live is not None:
                live.offer(frame for frame in frames if frame["seq"] > previous)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass


async def run(args):
    store = RawBuffer()
    decoder = DBCDecoder(args.dbc, parse_bus_dbcs(args.dbc_bus))
    live = None
    if args.grafana_live_url:
        # DBC definitions are read-only; separate counters avoid sharing decoder
        # state between the historical thread and the dedicated live thread.
        live = GrafanaLivePublisher(
            copy.copy(decoder), args.grafana_live_url, args.grafana_live_token,
            flush_seconds=args.live_flush_ms / 1000,
            batch_size=args.live_batch_size, queue_size=args.live_queue_size,
            max_age_seconds=args.live_max_age_seconds)
    transformer = None
    if args.transform_policy:
        transformer = TransformEngine(args.transform_policy, args.session_file)
        print(f"[transform] policy={args.transform_policy} "
              f"sessions={args.session_file or 'unassigned'}", flush=True)
    print(f"[server] {store.quick_status()}", flush=True)
    try:
        server = await asyncio.start_server(
            lambda reader, writer: serve_client(reader, writer, store, args.token, live),
            args.host, args.port, limit=1024 * 1024)
        print(f"[server] listening on {args.host}:{args.port}", flush=True)
        async with server:
            async with asyncio.TaskGroup() as group:
                group.create_task(server.serve_forever())
                if live is not None:
                    group.create_task(live.run())
                if args.influx_url:
                    group.create_task(export_forever(
                        store, decoder, args.influx_url, args.influx_token,
                        transformer=transformer))
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="RAM-batched raw-CAN receiver and decoder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("TELEMETRY_TOKEN"),
                        help="shared demo token (or TELEMETRY_TOKEN environment variable)")
    parser.add_argument("--dbc", action="append", default=[], help="DBC path; repeat for priority order")
    parser.add_argument(
        "--dbc-bus", action="append", default=[], metavar="BUS=PATH",
        help="DBC restricted to one capture bus; repeat in decode priority order")
    parser.add_argument("--influx-url", help="full InfluxDB v2 write URL with precision=ns")
    parser.add_argument("--influx-token", default=os.getenv("INFLUXDB_TOKEN"))
    parser.add_argument("--grafana-live-url", default=os.getenv("GRAFANA_LIVE_URL"),
                        help="Grafana /api/live/push/trevcan HTTP endpoint")
    parser.add_argument("--grafana-live-token", default=os.getenv("GRAFANA_LIVE_TOKEN"),
                        help="Grafana service-account token (prefer the environment)")
    parser.add_argument("--live-flush-ms", type=float, default=20,
                        help="maximum normal live batching delay in milliseconds")
    parser.add_argument("--live-batch-size", type=int, default=256)
    parser.add_argument("--live-queue-size", type=int, default=8192)
    parser.add_argument("--live-max-age-seconds", type=float, default=2,
                        help="exclude stale samples from Live")
    parser.add_argument(
        "--transform-policy", type=Path,
        help="JSON policy file for timestamp-based live derived signals")
    parser.add_argument(
        "--session-file", type=Path,
        help="test-session catalog shared with TREVCAN Telemetry Tools")
    args = parser.parse_args()
    if (not math.isfinite(args.live_flush_ms) or args.live_flush_ms <= 0 or
            args.live_batch_size < 1 or args.live_queue_size < 1 or
            not math.isfinite(args.live_max_age_seconds) or args.live_max_age_seconds <= 0):
        parser.error("live flush, batch, queue, and age settings must be positive")
    if args.grafana_live_url and not args.grafana_live_token:
        parser.error("GRAFANA_LIVE_TOKEN is required when Grafana Live is enabled")
    try:
        parse_bus_dbcs(args.dbc_bus)
        if args.grafana_live_url:
            stream_namespace(args.grafana_live_url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        parser.error("a TELEMETRY_TOKEN is required when listening beyond localhost")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
