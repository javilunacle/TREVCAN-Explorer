"""Small, independent Grafana Live batches alongside InfluxDB recording.

The queue contains newly accepted frames. Stale/overflowed samples are counted
and discarded rather than delaying current vehicle observations. InfluxDB is
an independent best-effort recorder, with no on-disk backlog.
"""

import asyncio
import http.client
import math
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def live_measurement(bus, can_id, is_extended):
    """Stable, bus-aware channel path, independent of signal value or car ID."""
    return f"{bus}_{can_id:08x}_{'ext' if is_extended else 'std'}"


def stream_namespace(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"(.*/api/live/push/)([A-Za-z0-9_-]+)", parsed.path)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or not match
            or len(match[2]) > 64):
        raise ValueError("Grafana Live URL must be an HTTP(S) /api/live/push/STREAM endpoint")
    return match[2]


def _tag(value):
    return (str(value).replace("\r", " ").replace("\n", " ")
            .replace("\\", "\\\\").replace(" ", "\\ ")
            .replace(",", "\\,").replace("=", "\\="))


def _string(value):
    return '"' + (str(value).replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\r", " ").replace("\n", " ")) + '"'


def live_lines(frames, decoder):
    """One raw record and one wide decoded record per CAN sample, without reduction."""
    lines = []
    for frame in frames:
        if isinstance(frame["data"], str):
            frame = {**frame, "data": bytes.fromhex(frame["data"])}
        decoded = decoder.decode(frame)
        dbc, message, signals = decoded if decoded else ("", "Unknown", {})
        bus = frame.get("bus", "unknown")
        fields = [f"seq={frame['seq']}i", f"dlc={frame['dlc']}i",
                  f"data={_string(frame['data'].hex().upper())}",
                  f"extended={str(frame['is_extended']).lower()}",
                  f"remote={str(frame['is_remote']).lower()}",
                  f"fd={str(frame['is_fd']).lower()}",
                  f"error={str(frame['is_error']).lower()}"]
        summary = "; ".join(f"{name}={value}" for name, value in signals.items())
        can_id = f"0x{frame['can_id']:X}"
        lines.append(
            f"can_frames car={_string(frame['car_id'])},bus={_string(bus)},"
            f"can_id={_string(can_id)},dbc={_string(dbc)},"
            f"message={_string(message)},decoded={_string(summary)},"
            f"{','.join(fields)} {frame['timestamp_ns']}")
        if decoded is None:
            continue
        signal_fields = []
        for name, value in signals.items():
            if hasattr(value, "name") and hasattr(value, "value"):
                signal_fields.append(f"{_tag('state.' + name)}={_string(value.name)}")
                value = value.value
            if isinstance(value, (int, float)):
                number = float(value)
                if math.isfinite(number):
                    signal_fields.append(f"{_tag('signal.' + name)}={number}")
            else:
                signal_fields.append(f"{_tag('state.' + name)}={_string(value)}")
        if signal_fields:
            measurement = live_measurement(bus, frame["can_id"], frame["is_extended"])
            lines.append(
                f"{measurement},car={_tag(frame['car_id'])} "
                f"{','.join(signal_fields)} {frame['timestamp_ns']}")
    return "\n".join(lines) + ("\n" if lines else "")


class GrafanaPushError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Grafana Live returned HTTP {status}")


class GrafanaHTTPClient:
    """Keep-alive HTTP push connection, used only by the dedicated live thread."""

    def __init__(self, url, token, timeout=1):
        stream_namespace(url)
        self.url = urlsplit(url)
        self.token = token
        self.timeout = timeout
        self.connections = {}

    def post(self, payload, *, synthetic=False):
        connection = self.connections.get(synthetic)
        if connection is None:
            connection_type = (http.client.HTTPSConnection if self.url.scheme == "https"
                               else http.client.HTTPConnection)
            connection = connection_type(self.url.hostname, self.url.port, timeout=self.timeout)
            self.connections[synthetic] = connection
        path = self.url.path + ("_synthetic" if synthetic else "")
        try:
            connection.request("POST", path, payload.encode("utf-8"), {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "text/plain; charset=utf-8",
            })
            response = connection.getresponse()
            response.read()  # Complete the response before reusing the connection.
            if response.status >= 300:
                raise GrafanaPushError(response.status)
        except Exception:
            connection.close()
            self.connections.pop(synthetic, None)
            raise

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()


class GrafanaLivePublisher:
    def __init__(self, decoder, url, token, *, flush_seconds=0.02,
                 batch_size=256, queue_size=8192, max_age_seconds=2):
        self.decoder = decoder
        self.client = GrafanaHTTPClient(url, token)
        self.flush_seconds = flush_seconds
        self.batch_size = batch_size
        self.queue_size = queue_size
        self.max_age_ns = int(max_age_seconds * 1_000_000_000)
        self.queue = deque()
        self.accepted = self.sent = self.stale = self.overflow = self.failed = 0
        self.push_failures = 0

    def offer(self, frames):
        """Called after RAM acceptance/ACK; never waits for a network write."""
        now_ns = time.time_ns()
        for frame in frames:
            if not -self.max_age_ns <= now_ns - frame["timestamp_ns"] <= self.max_age_ns:
                self.stale += 1
                continue
            if len(self.queue) >= self.queue_size:
                self.queue.popleft()
                self.overflow += 1
            self.queue.append(frame)
            self.accepted += 1

    def _send(self, frames):
        now_ns = time.time_ns()
        fresh = [frame for frame in frames
                 if -self.max_age_ns <= now_ns - frame["timestamp_ns"] <= self.max_age_ns]
        stale = len(frames) - len(fresh)
        sent = failed = 0
        # Synthetic validation must never enter the normal vehicle channels.
        for synthetic in (False, True):
            selected = [frame for frame in fresh
                        if frame["car_id"].startswith("scenario-") == synthetic]
            if not selected:
                continue
            try:
                self.client.post(live_lines(selected, self.decoder), synthetic=synthetic)
                sent += len(selected)
            except Exception as exc:
                failed += len(selected)
                self.push_failures += 1
                # Do not log the URL, request, or response body: they may contain secrets.
                if self.push_failures <= 5 or self.push_failures % 1000 == 0:
                    reason = (f"HTTP {exc.status}" if isinstance(exc, GrafanaPushError)
                              else type(exc).__name__)
                    print(f"[grafana-live] push failed ({reason}); "
                          f"{len(selected)} live frames discarded; history runs independently", flush=True)
        return sent, stale, failed

    def status(self):
        return {"queued": len(self.queue), "accepted": self.accepted,
                "sent": self.sent, "stale": self.stale,
                "overflow": self.overflow, "failed": self.failed}

    async def run(self, stop=None):
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grafana-live")
        last_status = time.monotonic()
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), self.flush_seconds)
                    break
                except asyncio.TimeoutError:
                    pass
                frames = [self.queue.popleft()
                          for _ in range(min(len(self.queue), self.batch_size))]
                if frames:
                    sent, stale, failed = await loop.run_in_executor(executor, self._send, frames)
                    self.sent += sent
                    self.stale += stale
                    self.failed += failed
                if time.monotonic() - last_status >= 5:
                    print(f"[grafana-live] {self.status()}", flush=True)
                    last_status = time.monotonic()
        finally:
            # A cancelled task may still have a request in flight. Queue close
            # behind it, on the same executor, instead of racing its socket.
            await loop.run_in_executor(executor, self.client.close)
            executor.shutdown(wait=False)
