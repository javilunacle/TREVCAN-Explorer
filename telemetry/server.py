"""RAM-batched CAN receiver with independent Live and InfluxDB publishing."""

import argparse
import asyncio
import json
import math
import os
import urllib.request
from pathlib import Path

from .common import point_line, wire_line
from .buffers import RawBuffer
from .live import GrafanaLivePublisher, stream_namespace
from .transforms import TransformEngine




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


_TABLE_ROW_SEPARATOR = "\u241f"


def _table_row_value(*values):
    """Pack display-only columns without creating six Flux field streams."""
    return _TABLE_ROW_SEPARATOR.join(
        str(value).replace(_TABLE_ROW_SEPARATOR, " ").replace("\r", " ").replace("\n", " ")
        for value in values
    )




def prepare_influx_history(frames):
    """Format every prepared frame and numeric sample at its capture timestamp."""
    lines = []
    for frame in frames:
        signals = frame["decoded"]
        labels = frame.get("labels", {})
        dbc = frame["dbc"] or "Unknown / undecoded"
        message = frame["message"] or "Unknown"
        summary = "; ".join(
            f"{name}={labels[name]} ({value})" if name in labels else f"{name}={value}"
            for name, value in signals.items())
        # Keep the historical `car` tag for existing dashboards; it is a stream
        # ID. Each frame keeps its own timestamp and sequence, even inside a batch.
        tags = {"car": frame["stream_id"], "bus": frame.get("bus", "unknown"),
                "can_id": f"0x{frame['can_id']:X}"}
        data = frame["data"].hex().upper()
        fields = {"seq": frame["seq"], "dlc": frame["dlc"], "data": data,
                  "remote": frame["is_remote"], "fd": frame["is_fd"],
                  "error": frame["is_error"], "message": message, "dbc": dbc,
                  "decoded": summary,
                  "table_row": _table_row_value(
                      frame["seq"], frame["dlc"], data, dbc, message, summary)}
        fields.update({f"b{i}": byte for i, byte in enumerate(frame["data"])})
        lines.append(point_line({
            "measurement": "can_frame", "fields": fields,
            "tags": {**tags, "ext": str(frame["is_extended"]).lower()},
            "timestamp_ns": frame["timestamp_ns"],
        }))
        # Record every numeric decoded update, not just the last one in a batch.
        for name, value in signals.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            fields = {"seq": frame["seq"], "value": float(value)}
            if name in labels:
                fields["state"] = labels[name]
            lines.append(point_line({
                "measurement": "can_signal", "fields": fields,
                "tags": {**tags, "message": message, "signal": name, "dbc": dbc},
                "timestamp_ns": frame["timestamp_ns"],
            }))
        # Calculations already ran once on acceptance. Both output workers use
        # these same results, independently of network success or write latency.
        lines.extend(point_line(point) for point in frame["calculated"])
    return "\n".join(lines) + ("\n" if lines else "")


def post_influx(url, token, payload):
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Token {token}"
    request = urllib.request.Request(url, data=payload.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"InfluxDB returned HTTP {response.status}")


async def export_forever(store, url, token, stop=None, *, batch_size=5000,
                         interval_seconds=0.1):
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
            payload = await asyncio.to_thread(prepare_influx_history, batch)
            await asyncio.to_thread(post_influx, url, token, payload)
            store.finish_export(batch)
        except Exception as exc:
            store.discard_pending()
            print(f"[influx] export failed: {exc}; disconnected data discarded", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass
            store.discard_pending()  # Resume with samples arriving after the retry pause.




def prepare_frame(frame, decoder=None, transformer=None, *, session_catalog=None):
    """Use the supplied decode, then calculate once before output fan-out."""
    if "decoded" not in frame:
        # Raw-only diagnostic/synthetic senders still work. CANManager records
        # already have this field, even when empty, and never enter this decode.
        result = decoder.decode(frame) if decoder is not None else None
        dbc, message, signals = result if result else ("", "", {})
        frame["dbc"], frame["message"] = dbc, message
        frame["labels"] = {name: value.name for name, value in signals.items()
                           if hasattr(value, "name") and hasattr(value, "value")}
        frame["decoded"] = {
            name: value.value if hasattr(value, "value") else value
            for name, value in signals.items()}
    frame.setdefault("dbc", "")
    frame.setdefault("message", "")
    frame.setdefault("labels", {})
    frame["calculated"] = []
    if transformer is not None:
        frame["calculated"] = transformer.process_frame(
            frame, session_catalog=session_catalog)


async def serve_client(reader, writer, store, token, live=None, *, decoder=None,
                       transformer=None):
    transform_failures = 0
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
                # Validate the whole batch and reject duplicate sequences before
                # updating calculations. The returned objects are also in RawBuffer.
                seq, accepted = store.commit_batch(frames)
                catalog = transformer.sessions.catalog() if transformer else None
                for frame in accepted:
                    try:
                        prepare_frame(frame, decoder, transformer, session_catalog=catalog)
                    except Exception as exc:
                        # Keep raw/decoded observations if one calculation fails.
                        # Do not retry this input and accidentally integrate it twice.
                        transform_failures += 1
                        if transform_failures <= 5 or transform_failures % 1000 == 0:
                            print(f"[transform] skipped seq={frame['seq']}: {exc}", flush=True)
                writer.write(wire_line({"type": "ack", "seq": seq}))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                writer.write(wire_line({"type": "error", "error": str(exc)}))
                await writer.drain()
                break
            await writer.drain()
            if live is not None:
                live.offer(accepted)
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
        # Output workers only format/send prepared samples. They never decode
        # or calculate, and Live does not wait for the InfluxDB writer.
        live = GrafanaLivePublisher(
            args.grafana_live_url, args.grafana_live_token,
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
            lambda reader, writer: serve_client(
                reader, writer, store, args.token, live,
                decoder=decoder, transformer=transformer),
            args.host, args.port, limit=1024 * 1024)
        print(f"[server] listening on {args.host}:{args.port}", flush=True)
        async with server:
            async with asyncio.TaskGroup() as group:
                group.create_task(server.serve_forever())
                if live is not None:
                    group.create_task(live.run())
                if args.influx_url:
                    group.create_task(export_forever(
                        store, args.influx_url, args.influx_token))
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
