"""CAN decoding and RAM-batched transport tests; no external services required."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from .buffers import CarBuffer, RawBuffer
from .car import capture_forever, capture_writer, load_simulation_frames, send_forever
from .server import (DBCDecoder, export_forever, parse_bus_dbcs,
                     post_influx, prepare_influx_history, serve_client)
from .simulate import DBC_DIRECTORY, DEFAULT_DBC_FILES, generate_frames


def _history_lines(frames, decoder):
    return prepare_influx_history(frames, decoder)[0]


async def _wait_for(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 5)


class DecodeTests(unittest.TestCase):
    def test_full_dashboard_replay_covers_enabled_telemetry_without_control_frames(self):
        paths = [DBC_DIRECTORY / name for name in DEFAULT_DBC_FILES]
        frames = generate_frames(paths)
        identities = {(f["can_id"], f["is_extended"]) for f in frames}
        self.assertGreater(len(identities), 250)
        self.assertEqual(len(frames), len(identities) * 2)
        names = {frame["message"] for frame in frames}
        for name in ("BMS_Heartbeat_0", "CellVoltage_m0_cellgrp1_to_cellgrp3",
                     "HVC_IO_VSense", "VCU_Summary", "MOBO_Heartbeat", "Temperatures_1",
                     "DBF_WSPD_FL", "ImuData"):
            self.assertIn(name, names)
        self.assertNotIn("MOBO_Reset_Command", names)
        decoder = DBCDecoder(paths)
        exported = []
        for frame in frames:
            raw = {"car_id": "synthetic-test", "seq": len(exported) + 1,
                   "timestamp_ns": (len(exported) + 1) * 1_000_000_000,
                   "can_id": frame["can_id"], "is_extended": frame["is_extended"],
                   "is_remote": False, "is_fd": frame["is_fd"], "is_error": False,
                   "dlc": frame["dlc"], "data": bytes.fromhex(frame["data"])}
            decoded = decoder.decode(raw)
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded[:2], (frame["source_dbc"], frame["message"]))
            exported.extend(_history_lines([raw], decoder).splitlines())
        self.assertEqual(sum(line.startswith("can_frame,") for line in exported), len(frames))
        for tag in ("signal=CellVoltage_m0_cellgrp1", "signal=HVC_Current_Low_A",
                    "signal=VCU_Speed_MPH", "signal=Battery_Voltage",
                    "signal=INV_Motor_Speed"):
            self.assertTrue(any(tag in line for line in exported), tag)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.jsonl"
            path.write_text("\n".join(json.dumps(frame) for frame in frames), encoding="utf-8")
            self.assertEqual(len(load_simulation_frames(path)), len(frames))

    def test_replay_rejects_bad_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text('{"can_id":160,"data":"00"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1"):
                load_simulation_frames(path)

    def test_server_side_dbc_decode_and_influx_points(self):
        dbc_path = Path(__file__).resolve().parents[1] / "telemetry/dbc_files/master.dbc"
        decoder = DBCDecoder([str(dbc_path)])
        frame = {"car_id": "test-car", "seq": 1, "timestamp_ns": 123456789,
                 "can_id": 160, "is_extended": False, "is_remote": False,
                 "is_fd": False, "is_error": False, "dlc": 8,
                 "data": (250).to_bytes(2, "little", signed=True) * 4}
        lines = _history_lines([frame], decoder).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith(
            "can_frame,car=test-car,bus=unknown,can_id=0xA0"))
        self.assertIn('message="INV_Temps_1"', lines[0])
        self.assertIn('decoded="INV_Module_A_Temp=25.0;', lines[0])
        self.assertTrue(any("signal=INV_Module_A_Temp" in line and "value=25.0" in line
                            for line in lines))

        unknown = {**frame, "can_id": 0x700, "seq": 2}
        unknown_lines = _history_lines([unknown], decoder).splitlines()
        self.assertEqual(len(unknown_lines), 1)
        self.assertIn('decoded=""', unknown_lines[0])

    def test_bus_aware_decoder_isolates_overlapping_can_namespaces(self):
        dbc_dir = Path(__file__).resolve().parents[1] / "telemetry/dbc_files"
        decoder = DBCDecoder(bus_paths={
            "can0": [str(dbc_dir / "VCU.dbc"), str(dbc_dir / "hvc.dbc")],
            "can1": [str(dbc_dir / "DAQ-Firmware.dbc")],
        })
        base = {"car_id": "test-car", "seq": 1, "timestamp_ns": 1,
                "is_extended": True, "is_remote": False, "is_fd": False,
                "is_error": False}

        vcu = decoder.decode({**base, "bus": "can0", "can_id": 0xC0,
                              "is_extended": False, "dlc": 8, "data": bytes(8)})
        self.assertEqual(vcu[:2], ("VCU.dbc", "VCU_INV_Command"))
        self.assertIsNone(decoder.decode(
            {**base, "bus": "can1", "can_id": 0xC0, "is_extended": False,
             "dlc": 8, "data": bytes(8)}))

        daq = decoder.decode({**base, "bus": "can1", "can_id": 0xDA10000,
                              "dlc": 8, "data": bytes(8)})
        self.assertEqual(daq[:2], ("DAQ-Firmware.dbc", "DBF_WSPD_FL"))
        self.assertIsNone(decoder.decode(
            {**base, "bus": "can0", "can_id": 0xDA10000,
             "dlc": 8, "data": bytes(8)}))

        hvc = decoder.decode({**base, "bus": "can0", "can_id": 0x4001F3,
                              "dlc": 6, "data": bytes(6)})
        self.assertEqual(hvc[:2], ("hvc.dbc", "HVC_IO_Current"))

    def test_bus_dbc_argument_parser_preserves_priority(self):
        self.assertEqual(parse_bus_dbcs([
            "can0=first.dbc", "can1=other.dbc", "can0=second.dbc",
        ]), {"can0": ["first.dbc", "second.dbc"], "can1": ["other.dbc"]})
        with self.assertRaisesRegex(ValueError, "expected BUS=PATH"):
            parse_bus_dbcs(["missing-separator"])

    def test_decode_failure_identifies_bus_id_dlc_and_expected_length(self):
        dbc_path = (Path(__file__).resolve().parents[1] /
                    "telemetry/dbc_files/hvc.dbc")
        decoder = DBCDecoder(bus_paths={"can0": [str(dbc_path)]})
        frame = {"car_id": "test-car", "seq": 42, "timestamp_ns": 1,
                 "bus": "can0", "can_id": 0x4001F3, "is_extended": True,
                 "is_remote": False, "is_fd": False, "is_error": False,
                 "dlc": 5, "data": bytes(5)}
        with patch("builtins.print") as output:
            self.assertIsNone(decoder.decode(frame))
        diagnostic = output.call_args.args[0]
        self.assertIn("bus=can0 can_id=0x4001F3 dlc=5 seq=42", diagnostic)
        self.assertIn("HVC_IO_Current[expected_dlc=6]", diagnostic)

    def test_influx_http_write_shape(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            post_influx(f"http://127.0.0.1:{server.server_port}/api/v2/write?precision=ns",
                        "test-token", "can_frame,car=test seq=1i 123\n")
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "/api/v2/write?precision=ns")
            self.assertEqual(received[0][1], "Token test-token")
            self.assertEqual(received[0][2], b"can_frame,car=test seq=1i 123\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_preserves_1024_samples_in_four_small_posts(self):
        store = RawBuffer(max_age_seconds=10)
        decoder = DBCDecoder([DBC_DIRECTORY / "master.dbc"])
        frames = [{"type": "frame", "car_id": "batch-test", "seq": seq,
                   "timestamp_ns": time.time_ns() + seq, "bus": "can0",
                   "can_id": 160, "is_extended": False, "is_remote": False,
                   "is_fd": False, "is_error": False, "dlc": 8,
                   "data": "FA00000000000000"} for seq in range(1, 1025)]
        store.commit_batch(frames)
        stop = asyncio.Event()
        posts = []
        with patch("telemetry.server.post_influx",
                   side_effect=lambda url, token, body: posts.append(body)):
            task = asyncio.create_task(export_forever(
                store, decoder, "http://unused", None, stop,
                batch_size=256, interval_seconds=0.001))
            try:
                await _wait_for(lambda: store.status()["pending_influx"] == 0)
                self.assertEqual(len(posts), 4)
                self.assertTrue(all(body.count("can_frame,") == 256 for body in posts))
                self.assertTrue(all(body.count("can_signal,") == 1024 for body in posts))
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_1024_frames_cross_network_in_four_ram_batches(self):
        buffer = CarBuffer("batch-test")
        store = RawBuffer(max_age_seconds=10)
        stop = asyncio.Event()
        server = await asyncio.start_server(
            lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0,
            limit=1024 * 1024)
        port = server.sockets[0].getsockname()[1]
        with patch.object(store, "commit_batch", wraps=store.commit_batch) as accepts:
            sender = asyncio.create_task(send_forever(
                buffer, "127.0.0.1", port, stop=stop, window_size=256))
            try:
                await _wait_for(lambda: buffer.connected)
                buffer.append_many([
                    (f"can{index % 2}", 0x100 + index % 8, b"12345678",
                     False, False, False, False, 8, time.time_ns())
                    for index in range(1024)])
                await _wait_for(lambda: buffer.last_acked == 1024)
                self.assertEqual(accepts.call_count, 4)
                self.assertEqual(store.received, 1024)
                self.assertEqual({item["bus"] for item in store.pending()}, {"can0", "can1"})
            finally:
                stop.set()
                await asyncio.wait_for(sender, 3)
                server.close()
                await asyncio.wait_for(server.wait_closed(), 2)

    async def test_dashboard_samples_cross_tcp_and_historical_export(self):
        paths = [DBC_DIRECTORY / name for name in DEFAULT_DBC_FILES]
        names = {"BMS_Heartbeat_0", "HVC_IO_VSense", "Temperatures_1",
                 "VCU_Summary", "MOBO_Heartbeat"}
        samples = [item for item in generate_frames(paths, cycles=1)
                   if item["message"] in names]
        self.assertEqual(len(samples), len(names))
        decoder = DBCDecoder(paths)
        buffer = CarBuffer("dashboard-test")
        store = RawBuffer(max_age_seconds=10)
        stop = asyncio.Event()
        server = await asyncio.start_server(
            lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        posts = []
        with patch("telemetry.server.post_influx",
                   side_effect=lambda url, token, body: posts.append(body)):
            sender = asyncio.create_task(send_forever(buffer, "127.0.0.1", port, stop=stop))
            exporter = asyncio.create_task(export_forever(
                store, decoder, "http://unused", None, stop))
            try:
                await _wait_for(lambda: buffer.connected)
                for item in samples:
                    buffer.append(item["can_id"], bytes.fromhex(item["data"]),
                                  bus=item["bus"], is_extended=item["is_extended"])
                await _wait_for(lambda: store.received == len(samples) and
                                store.status()["pending_influx"] == 0 and
                                buffer.status()["queued"] == 0)
                body = "".join(posts)
                for name in names:
                    self.assertIn(f'message="{name}"', body)
            finally:
                stop.set()
                await asyncio.wait_for(asyncio.gather(sender, exporter), 3)
                server.close()
                await asyncio.wait_for(server.wait_closed(), 2)

    async def test_integrated_simulation_to_decoded_export(self):
        buffer = CarBuffer("simulation")
        store = RawBuffer(max_age_seconds=10)
        decoder = DBCDecoder([DBC_DIRECTORY / "master.dbc"])
        queue = asyncio.Queue(maxsize=10)
        stop = asyncio.Event()
        server = await asyncio.start_server(
            lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        posts = []
        with patch("telemetry.server.post_influx",
                   side_effect=lambda url, token, body: posts.append(body)):
            sender = asyncio.create_task(send_forever(buffer, "127.0.0.1", port, stop=stop))
            exporter = asyncio.create_task(export_forever(
                store, decoder, "http://unused", None, stop))
            capture = asyncio.create_task(capture_forever(queue, simulate_rate=20))
            writer = asyncio.create_task(capture_writer(buffer, queue))
            try:
                await _wait_for(lambda: store.received >= 3 and
                                store.status()["pending_influx"] == 0)
                self.assertTrue(any("can_frame," in body and "can_signal," in body
                                    for body in posts))
            finally:
                capture.cancel()
                writer.cancel()
                await asyncio.gather(capture, writer, return_exceptions=True)
                stop.set()
                await asyncio.wait_for(asyncio.gather(sender, exporter), 3)
                server.close()
                await asyncio.wait_for(server.wait_closed(), 2)

    async def test_raw_replay_loops_without_can_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.jsonl"
            frame = {"can_id": 160, "is_extended": False, "is_remote": False,
                     "is_fd": False, "is_error": False, "dlc": 8,
                     "data": "FA00000000000000"}
            path.write_text(json.dumps(frame) + "\n", encoding="utf-8")
            queue = asyncio.Queue(maxsize=2)
            task = asyncio.create_task(capture_forever(queue, simulate_file=path, replay_rate=100))
            try:
                first = await asyncio.wait_for(queue.get(), 1)
                second = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(first[:7], second[:7])
                self.assertGreaterEqual(second[7], first[7])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_simulator_emits_raw_master_dbc_frame(self):
        queue = asyncio.Queue(maxsize=2)
        task = asyncio.create_task(capture_forever(queue, simulate_rate=20))
        try:
            bus, can_id, data, extended, remote, fd, error, dlc, timestamp_ns = await asyncio.wait_for(queue.get(), 2)
            self.assertEqual(bus, "simulation")
            self.assertEqual((can_id, dlc), (160, 8))
            self.assertEqual((extended, remote, fd, error), (False, False, False, False))
            self.assertGreater(timestamp_ns, 0)
            self.assertEqual(int.from_bytes(data[:2], "little", signed=True), 250)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
