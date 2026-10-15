"""Live/history isolation and sample preservation without a running Grafana."""

import asyncio
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from .common import wire_line
from .live import GrafanaHTTPClient, GrafanaLivePublisher, live_lines, live_measurement
from .buffers import RawBuffer
from .server import export_forever, prepare_influx_history, serve_client
from .grafana.live_dashboard import channel_catalog, live_dashboard


class Decoder:
    def decode(self, frame):
        if frame["can_id"] == 0x700:
            return None
        return "test.dbc", "Test", {"Speed": float(frame["seq"])}


def frame(seq, *, car="test-car", bus="can0", timestamp=None, can_id=160):
    return {"type": "frame", "car_id": car, "bus": bus, "seq": seq,
            "timestamp_ns": timestamp or time.time_ns(), "can_id": can_id,
            "is_extended": False, "is_remote": False, "is_fd": False,
            "is_error": False, "dlc": 8, "data": "0000000000000000"}


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 5)


class LiveDataTests(unittest.TestCase):
    def test_live_and_history_keep_every_intermediate_sample_and_source_time(self):
        frames = [frame(seq, timestamp=1_000_000_000 + seq) for seq in range(1, 101)]
        live = live_lines(frames, Decoder()).splitlines()
        self.assertEqual(len(live), 200)
        samples = [line for line in live if line.startswith("can0_")]
        self.assertEqual(len(samples), 100)
        self.assertEqual([line.split()[-1] for line in samples],
                         [str(item["timestamp_ns"]) for item in frames])
        self.assertTrue(any("signal.Speed=50.0 " in line for line in samples))
        stored = [{**item, "data": bytes.fromhex(item["data"])} for item in frames]
        history, _ = prepare_influx_history(stored, Decoder())
        self.assertEqual(history.count("can_frame,"), 100)
        self.assertEqual(history.count("can_signal,"), 100)
        self.assertIn("seq=50i,value=50.0 1000000050", history)
        self.assertIn('table_row="50', history)

    def test_unknown_frames_remain_visible_and_buses_have_separate_channels(self):
        lines = live_lines([frame(1), frame(2, bus="can1"),
                            frame(3, can_id=0x700)], Decoder())
        self.assertEqual(lines.count("can_frames "), 3)
        self.assertIn("can0_000000a0_std,", lines)
        self.assertIn("can1_000000a0_std,", lines)
        self.assertIn('message="Unknown"', lines)
        self.assertNotEqual(live_measurement("can0", 160, False),
                            live_measurement("can0", 160, True))

    def test_nonfinite_signals_cannot_poison_a_live_batch(self):
        class NonfiniteDecoder:
            def decode(self, item):
                return "test.dbc", "Test", {"Good": 1.0, "Bad": float("nan")}
        lines = live_lines([frame(1)], NonfiniteDecoder())
        self.assertIn("signal.Good=1.0", lines)
        self.assertNotIn("signal.Bad=", lines)
        raw = {**frame(1), "data": bytes(8)}
        history, _ = prepare_influx_history([raw], NonfiniteDecoder())
        self.assertIn("signal=Good", history)
        self.assertNotIn("signal=Bad", history)
        self.assertIn("can_frame,", history)

    def test_stale_frames_and_overflow_are_counted_without_latest_value_reduction(self):
        publisher = GrafanaLivePublisher(Decoder(), "http://localhost/api/live/push/trevcan",
                                        "secret", queue_size=2)
        now = time.time_ns()
        publisher.offer([frame(1, timestamp=now - 3_000_000_000),
                         frame(2), frame(3), frame(4),
                         frame(5, timestamp=now + 3_000_000_000)])
        self.assertEqual(publisher.stale, 2)
        self.assertEqual(publisher.overflow, 1)
        self.assertEqual([item["seq"] for item in publisher.queue], [3, 4])

    def test_live_dashboard_uses_only_streaming_targets_and_static_selectors(self):
        dashboard = live_dashboard()
        self.assertEqual(dashboard["refresh"], "")
        self.assertTrue(dashboard["liveNow"])
        for panel in dashboard["panels"]:
            self.assertEqual(panel["datasource"]["uid"], "grafana")
            for target in panel["targets"]:
                self.assertEqual(target["queryType"], "measurements")
                self.assertTrue(target["channel"].startswith("stream/trevcan/"))
                self.assertNotIn("query", target)
        self.assertTrue(all(variable["type"] == "custom"
                            for variable in dashboard["templating"]["list"]))
        catalog = channel_catalog()
        self.assertEqual(len({item["channel"] for item in catalog}), len(catalog))
        self.assertTrue({"can0", "can1"}.issubset({item["bus"] for item in catalog}))

    def test_http_push_auth_and_synthetic_namespace(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append((self.path, self.headers.get("Authorization"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = GrafanaHTTPClient(f"http://127.0.0.1:{server.server_port}/api/live/push/trevcan",
                                  "publisher-token")
        try:
            client.post("sample value=1.0 123\n")
            client.post("sample value=2.0 124\n", synthetic=True)
            self.assertEqual([item[0] for item in requests],
                             ["/api/live/push/trevcan", "/api/live/push/trevcan_synthetic"])
            self.assertEqual(requests[0][1], "Bearer publisher-token")
            self.assertEqual(requests[0][2], b"sample value=1.0 123\n")
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class LiveIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = RawBuffer()
        self.live = GrafanaLivePublisher(Decoder(), "http://localhost/api/live/push/trevcan",
                                        "secret", flush_seconds=0.001)
        self.stop = asyncio.Event()
        self.tasks = []
        self.server = await asyncio.start_server(
            lambda r, w: serve_client(r, w, self.store, "transport-token", self.live),
            "127.0.0.1", 0)
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        self.stop.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), 5)
        self.writer.close()
        await self.writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(0)
        self.store.close()

    async def send(self, frames):
        self.writer.write(wire_line({"type": "batch", "frames": frames,
                                     "token": "transport-token"}))
        await self.writer.drain()
        return await asyncio.wait_for(self.reader.readline(), 2)

    async def test_influx_failure_does_not_block_live_and_duplicate_acks_do_not_republish(self):
        posts = []
        with (patch.object(self.live.client, "post", side_effect=lambda body, **kw: posts.append(body)),
              patch("telemetry.server.post_influx", side_effect=RuntimeError("offline"))):
            self.tasks = [asyncio.create_task(self.live.run(self.stop)),
                          asyncio.create_task(export_forever(
                              self.store, Decoder(), "http://unused", None, self.stop,
                              interval_seconds=0.001))]
            frames = [frame(seq) for seq in range(1, 101)]
            self.assertIn(b'"seq":100', await self.send(frames))
            await eventually(lambda: self.live.sent == 100)
            self.assertIn(b'"seq":100', await self.send(frames))
            self.assertEqual(self.live.accepted, 100)
            self.assertEqual(sum(body.count("signal.Speed=") for body in posts), 100)
            self.assertEqual(self.store.status()["pending_influx"], 0)
            self.assertEqual(self.store.status()["dropped"], 100)
            self.stop.set()
            await asyncio.gather(*self.tasks)

    async def test_slow_live_push_does_not_block_ack_or_history(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_push(*args, **kwargs):
            entered.set()
            release.wait(5)

        with (patch.object(self.live.client, "post", side_effect=blocked_push),
              patch("telemetry.server.post_influx")):
            self.tasks = [asyncio.create_task(self.live.run(self.stop)),
                          asyncio.create_task(export_forever(
                              self.store, Decoder(), "http://unused", None, self.stop,
                              interval_seconds=0.001))]
            try:
                await self.send([frame(1)])
                await eventually(entered.is_set)
                self.assertIn(b'"seq":2', await self.send([frame(2)]))
                await eventually(lambda: self.store.status()["pending_influx"] == 0)
                self.assertFalse(release.is_set())
                self.assertEqual(self.store.status()["raw_frames"], 2)
            finally:
                release.set()
                self.stop.set()
                await asyncio.gather(*self.tasks)

    async def test_live_failure_leaves_the_independent_history_buffer_available(self):
        with patch.object(self.live.client, "post", side_effect=RuntimeError("offline")):
            self.tasks = [asyncio.create_task(self.live.run(self.stop))]
            await self.send([frame(1), frame(2)])
            await eventually(lambda: self.live.failed == 2)
            self.assertEqual(self.store.status()["pending_influx"], 2)
            self.assertEqual(len(self.store.pending()), 2)
            self.stop.set()
            await asyncio.gather(*self.tasks)

    async def test_synthetic_and_vehicle_samples_use_separate_pushes(self):
        posts = []
        with patch.object(self.live.client, "post",
                          side_effect=lambda body, **kw: posts.append((body, kw["synthetic"]))):
            self.tasks = [asyncio.create_task(self.live.run(self.stop))]
            await self.send([frame(1)])
            await self.send([frame(1, car="scenario-test")])
            await eventually(lambda: self.live.sent == 2)
            self.assertTrue(any(synthetic for _, synthetic in posts))
            self.assertTrue(any(not synthetic for _, synthetic in posts))
            for body, synthetic in posts:
                self.assertEqual("scenario-test" in body, synthetic)
            self.stop.set()
            await asyncio.gather(*self.tasks)


if __name__ == "__main__":
    unittest.main()
