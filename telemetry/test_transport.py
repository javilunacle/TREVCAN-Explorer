"""Local integration tests; no CAN adapter, InfluxDB, or Grafana required."""

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from .backfill import backfill
from .car import (CarSpool, capture_forever, load_simulation_frames, read_spool_status,
                  send_forever, spool_writer)
from .server import (DBCDecoder, RawStore, export_forever, influx_lines,
                     influx_snapshot_lines, post_influx, serve_client)
from .simulate import DBC_DIRECTORY, DEFAULT_DBC_FILES, generate_frames


class StoreTests(unittest.TestCase):
    def test_existing_car_spool_migrates_without_losing_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-car.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executemany("INSERT INTO meta VALUES (?,?)", [
                ("car_id", "legacy-car"), ("next_seq", "2"),
                ("last_timestamp_ns", "100"), ("last_acked", "0")])
            db.execute("""CREATE TABLE frames (
                seq INTEGER PRIMARY KEY, timestamp_ns INTEGER NOT NULL,
                can_id INTEGER NOT NULL, is_extended INTEGER NOT NULL,
                is_remote INTEGER NOT NULL, is_fd INTEGER NOT NULL,
                is_error INTEGER NOT NULL, dlc INTEGER NOT NULL, data BLOB NOT NULL)""")
            db.execute("INSERT INTO frames VALUES (1,100,256,0,0,0,0,8,?)",
                       (b"12345678",))
            db.commit()
            db.close()
            spool = CarSpool(path, "legacy-car")
            try:
                self.assertEqual(spool.pending()[0]["bus"], "can0")
                self.assertEqual(spool.status()["queued"], 1)
            finally:
                spool.close()

    def test_existing_server_archive_migrates_without_losing_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-server.sqlite3"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE raw_frames (
                car_id TEXT NOT NULL, seq INTEGER NOT NULL,
                timestamp_ns INTEGER NOT NULL, can_id INTEGER NOT NULL,
                is_extended INTEGER NOT NULL, is_remote INTEGER NOT NULL,
                is_fd INTEGER NOT NULL, is_error INTEGER NOT NULL,
                dlc INTEGER NOT NULL, data BLOB NOT NULL,
                influx_done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (car_id,seq))""")
            db.execute("CREATE TABLE streams (car_id TEXT PRIMARY KEY,last_seq INTEGER NOT NULL)")
            db.execute("INSERT INTO raw_frames VALUES ('legacy-car',1,100,256,0,0,0,0,8,?,0)",
                       (b"12345678",))
            db.execute("INSERT INTO streams VALUES ('legacy-car',1)")
            db.commit()
            db.close()
            store = RawStore(path)
            try:
                self.assertEqual(store.pending()[0]["bus"], "unknown")
                self.assertEqual(store.status()["raw_frames"], 1)
            finally:
                store.close()

    def test_full_dashboard_replay_covers_enabled_telemetry_without_control_frames(self):
        paths = [DBC_DIRECTORY / name for name in DEFAULT_DBC_FILES]
        frames = generate_frames(paths)
        self.assertEqual(len(frames), 542)
        self.assertEqual(len({(f["can_id"], f["is_extended"]) for f in frames}), 271)
        names = {frame["message"] for frame in frames}
        for name in ("BMS_Heartbeat_0", "CellVoltage_m0_cellgrp1_to_cellgrp3",
                     "IO_VSense", "VCU_Summary", "MOBO_Heartbeat", "Temperatures_1"):
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
            exported.extend(influx_lines([raw], decoder).splitlines())
        self.assertEqual(sum(line.startswith("can_frame,") for line in exported), len(frames))
        for tag in ("signal=CellVoltage_m0_cellgrp1", "signal=Current_Low_mA",
                    "signal=VCU_Speed", "signal=Battery_Voltage",
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

    def test_backfill_adds_summary_without_touching_raw_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.sqlite3"
            store = RawStore(path)
            frames = [
                {"type": "frame", "car_id": "test-car", "seq": seq,
                 "timestamp_ns": seq * 1_000_000_000, "can_id": can_id,
                 "is_extended": False, "is_remote": False, "is_fd": False,
                 "is_error": False, "dlc": 8, "data": "FA00000000000000"}
                for seq, can_id in ((1, 160), (2, 0x700))
            ]
            for frame in frames:
                store.commit_frame(frame)
            store.mark_exported(store.pending())
            before = store.status()
            store.close()
            dbc_path = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files/master.dbc"
            posts = []
            dry = backfill(path, [str(dbc_path)], write=False,
                           post=lambda *args: posts.append(args))
            self.assertEqual(dry, {"scanned": 2, "matched_dbc": 1, "written": 0, "batches": 0})
            self.assertEqual(posts, [])
            result = backfill(path, [str(dbc_path)], "http://unused", "test-token",
                              write=True, post=lambda *args: posts.append(args))
            self.assertEqual(result["written"], 1)
            self.assertEqual(len(posts), 1)
            self.assertIn('decoded="INV_Module_A_Temp=', posts[0][2])
            self.assertNotIn("can_signal,", posts[0][2])
            reopened = RawStore(path)
            self.assertEqual(reopened.status(), before)
            reopened.close()

    def test_spool_survives_restart_and_acks_only_matching_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car.sqlite3"
            spool = CarSpool(path, "test-car")
            self.assertEqual(spool.append(160, b"12345678"), 1)
            self.assertEqual(spool.append(160, b"ABCDEFGH"), 2)
            with self.assertRaises(ValueError):
                spool.acknowledge(2)
            spool.close()
            reopened = CarSpool(path, "test-car")
            self.assertEqual(reopened.oldest()["seq"], 1)
            reopened.acknowledge(1)
            self.assertEqual(reopened.oldest()["seq"], 2)
            reopened.acknowledge(2)
            self.assertEqual(reopened.status()["last_acked"], 2)
            self.assertEqual(reopened.append(160, b"12345678"), 3)
            reopened.close()

    def test_spool_batches_bus_identity_and_cumulative_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car.sqlite3"
            spool = CarSpool(path, "test-car")
            records = [
                ("can0", 0x100, b"12345678", False, False, False, False, 8, 100 + index)
                if index % 2 == 0 else
                ("can1", 0x101, b"ABCDEFGH", False, False, False, False, 8, 100 + index)
                for index in range(4)
            ]
            self.assertEqual(spool.append_many(records), [1, 2, 3, 4])
            self.assertEqual([frame["bus"] for frame in spool.pending()],
                             ["can0", "can1", "can0", "can1"])
            spool.acknowledge_through(3)
            self.assertEqual([frame["seq"] for frame in spool.pending()], [4])
            self.assertEqual(spool.status()["last_acked"], 3)
            spool.close()

    def test_spool_rejects_a_second_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car.sqlite3"
            spool = CarSpool(path, "test-car")
            try:
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    CarSpool(path, "test-car")
            finally:
                spool.close()

    def test_status_is_read_only_while_writer_holds_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car.sqlite3"
            spool = CarSpool(path, "test-car")
            try:
                spool.append(0x100, b"12345678", bus="can1")
                status = read_spool_status(path)
                self.assertEqual(status["queued"], 1)
                self.assertEqual(status["queued_by_bus"], {"can1": 1})
            finally:
                spool.close()

    def test_receiver_is_duplicate_safe_and_rejects_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RawStore(Path(directory) / "server.sqlite3")
            frame = {"type": "frame", "car_id": "test-car", "seq": 1,
                     "timestamp_ns": 123456789, "can_id": 160,
                     "is_extended": False, "is_remote": False, "is_fd": False,
                     "is_error": False,
                     "dlc": 8, "data": "0000000000000000"}
            self.assertEqual(store.commit_frame(frame), 1)
            self.assertEqual(store.commit_frame(frame), 1)  # lost ACK, safe retransmission
            self.assertEqual(store.status()["raw_frames"], 1)
            with self.assertRaises(ValueError):
                store.commit_frame({**frame, "data": "0100000000000000"})
            with self.assertRaises(ValueError):
                store.commit_frame({**frame, "seq": 3})
            self.assertEqual(store.status()["streams"]["test-car"], 1)
            store.close()
            reopened = RawStore(Path(directory) / "server.sqlite3")
            self.assertEqual(reopened.status()["raw_frames"], 1)
            reopened.close()

    def test_receiver_commits_batch_and_preserves_bus(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RawStore(Path(directory) / "server.sqlite3")
            frames = [{"type": "frame", "car_id": "test-car", "seq": seq,
                       "timestamp_ns": 100 + seq, "bus": f"can{seq % 2}",
                       "can_id": 0x100 + seq, "is_extended": False,
                       "is_remote": False, "is_fd": False, "is_error": False,
                       "dlc": 8, "data": "0000000000000000"}
                      for seq in range(1, 5)]
            self.assertEqual(store.commit_batch(frames), 4)
            self.assertEqual([frame["bus"] for frame in store.pending()],
                             ["can1", "can0", "can1", "can0"])
            self.assertEqual(store.commit_batch(frames), 4)
            self.assertEqual(store.status()["raw_frames"], 4)
            store.close()

    def test_server_side_dbc_decode_and_influx_points(self):
        dbc_path = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files/master.dbc"
        decoder = DBCDecoder([str(dbc_path)])
        frame = {"car_id": "test-car", "seq": 1, "timestamp_ns": 123456789,
                 "can_id": 160, "is_extended": False, "is_remote": False,
                 "is_fd": False, "is_error": False, "dlc": 8,
                 "data": (250).to_bytes(2, "little", signed=True) * 4}
        lines = influx_lines([frame], decoder).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith(
            "can_frame,car=test-car,bus=unknown,can_id=0xA0"))
        self.assertIn('message="INV_Temps_1"', lines[0])
        self.assertIn('decoded="INV_Module_A_Temp=25.0;', lines[0])
        self.assertTrue(any("signal=INV_Module_A_Temp" in line and "value=25.0" in line
                            for line in lines))

        unknown = {**frame, "can_id": 0x700, "seq": 2}
        unknown_lines = influx_lines([unknown], decoder).splitlines()
        self.assertEqual(len(unknown_lines), 1)
        self.assertNotIn("decoded=", unknown_lines[0])

    def test_snapshot_keeps_latest_raw_frame_and_each_latest_signal(self):
        dbc_path = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files/master.dbc"
        decoder = DBCDecoder([str(dbc_path)])
        frames = [{"car_id": "test-car", "seq": seq, "timestamp_ns": 100 + seq,
                   "bus": "can0", "can_id": 160, "is_extended": False,
                   "is_remote": False, "is_fd": False, "is_error": False,
                   "dlc": 8, "data": (200 + seq).to_bytes(2, "little", signed=True) * 4}
                  for seq in range(1, 101)]
        lines = influx_snapshot_lines(frames, decoder).splitlines()
        self.assertEqual(sum(line.startswith("can_frame,") for line in lines), 1)
        self.assertEqual(sum(line.startswith("can_signal,") for line in lines), 4)
        self.assertTrue(all("seq=100i" in line for line in lines))

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
    async def test_snapshot_export_drains_10000_raw_rows_in_two_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RawStore(Path(directory) / "server.sqlite3")
            dbc_path = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files/master.dbc"
            decoder = DBCDecoder([str(dbc_path)])
            for start in range(1, 10001, 1000):
                frames = [{"type": "frame", "car_id": "backlog-test", "seq": seq,
                           "timestamp_ns": 1_000_000 + seq, "bus": "can0",
                           "can_id": 160, "is_extended": False, "is_remote": False,
                           "is_fd": False, "is_error": False, "dlc": 8,
                           "data": "FA00000000000000"}
                          for seq in range(start, start + 1000)]
                store.commit_batch(frames)
            stop = asyncio.Event()
            posts = []
            with patch("telemetry.server.post_influx",
                       side_effect=lambda url, token, body: posts.append(body)):
                task = asyncio.create_task(export_forever(
                    store, decoder, "http://unused", None, stop,
                    batch_size=5000, interval_seconds=0.001))
                try:
                    async def drained():
                        while store.status()["pending_influx"]:
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(drained(), 10)
                    self.assertEqual(len(posts), 2)
                    self.assertTrue(all(body.count("can_frame,") == 1 for body in posts))
                    self.assertTrue(all(body.count("can_signal,") == 4 for body in posts))
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 3)
                    store.close()

    async def test_1024_frames_cross_network_in_four_durable_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = CarSpool(Path(directory) / "car.sqlite3", "batch-test")
            store = RawStore(Path(directory) / "server.sqlite3")
            records = [(f"can{index % 2}", 0x100 + index % 8, b"12345678",
                        False, False, False, False, 8, 1_000_000 + index)
                       for index in range(1024)]
            spool.append_many(records)
            stop = asyncio.Event()
            server = await asyncio.start_server(
                lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0,
                limit=1024 * 1024)
            port = server.sockets[0].getsockname()[1]
            with patch.object(store, "commit_batch", wraps=store.commit_batch) as commits:
                sender = asyncio.create_task(send_forever(
                    spool, "127.0.0.1", port, stop=stop, window_size=256))
                try:
                    async def delivered():
                        while spool.status()["queued"] or store.status()["raw_frames"] < 1024:
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(delivered(), 10)
                    self.assertEqual(commits.call_count, 4)
                    self.assertEqual(store.status()["streams"]["batch-test"], 1024)
                finally:
                    stop.set()
                    await asyncio.wait_for(sender, 3)
                    server.close()
                    await server.wait_closed()
                    spool.close()
                    store.close()

    async def test_dashboard_samples_cross_spool_tcp_and_export(self):
        paths = [DBC_DIRECTORY / name for name in DEFAULT_DBC_FILES]
        names = {"BMS_Heartbeat_0", "IO_VSense", "Temperatures_1",
                 "VCU_Summary", "MOBO_Heartbeat"}
        samples = [frame for frame in generate_frames(paths, cycles=1)
                   if frame["message"] in names]
        self.assertEqual(len(samples), len(names))
        with tempfile.TemporaryDirectory() as directory:
            spool = CarSpool(Path(directory) / "car.sqlite3", "dashboard-test")
            store = RawStore(Path(directory) / "server.sqlite3")
            for frame in samples:
                spool.append(frame["can_id"], bytes.fromhex(frame["data"]),
                             is_extended=frame["is_extended"])
            stop = asyncio.Event()
            server = await asyncio.start_server(
                lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            posts = []
            with patch("telemetry.server.post_influx", side_effect=lambda url, token, body: posts.append(body)):
                sender = asyncio.create_task(send_forever(spool, "127.0.0.1", port, stop=stop))
                exporter = asyncio.create_task(export_forever(
                    store, DBCDecoder(paths), "http://unused", None, stop))
                try:
                    async def delivered():
                        while (store.status()["raw_frames"] != len(samples) or
                               store.status()["pending_influx"] != 0 or
                               spool.status()["queued"] != 0):
                            await asyncio.sleep(0.05)
                    await asyncio.wait_for(delivered(), 5)
                    body = "".join(posts)
                    for name in names:
                        self.assertIn(f'message="{name}"', body)
                    self.assertEqual(store.status()["raw_frames"], len(samples))
                finally:
                    stop.set()
                    await asyncio.wait_for(asyncio.gather(sender, exporter), 3)
                    server.close()
                    await server.wait_closed()
                    spool.close()
                    store.close()

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

    async def test_integrated_simulation_to_decoded_export(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = CarSpool(Path(directory) / "car.sqlite3", "test-car")
            store = RawStore(Path(directory) / "server.sqlite3")
            dbc_path = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files/master.dbc"
            decoder = DBCDecoder([str(dbc_path)])
            queue = asyncio.Queue(maxsize=10)
            stop = asyncio.Event()
            server = await asyncio.start_server(
                lambda r, w: serve_client(r, w, store, None), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            captured_posts = []
            with patch("telemetry.server.post_influx", side_effect=lambda url, token, body: captured_posts.append(body)):
                capture = asyncio.create_task(capture_forever(queue, simulate_rate=20))
                writer = asyncio.create_task(spool_writer(spool, queue))
                sender = asyncio.create_task(send_forever(spool, "127.0.0.1", port, stop=stop))
                exporter = asyncio.create_task(export_forever(store, decoder, "http://unused", None, stop))
                try:
                    async def exported():
                        while store.status()["raw_frames"] < 3 or store.status()["pending_influx"] != 0:
                            await asyncio.sleep(0.05)
                    await asyncio.wait_for(exported(), 5)
                    self.assertTrue(any("can_frame," in body and "can_signal," in body
                                        for body in captured_posts))
                    self.assertEqual(store.status()["raw_frames"], store.status()["streams"]["test-car"])
                finally:
                    capture.cancel()
                    writer.cancel()
                    await asyncio.gather(capture, writer, return_exceptions=True)
                    stop.set()
                    await asyncio.wait_for(asyncio.gather(sender, exporter), 3)
                    server.close()
                    await server.wait_closed()
                    spool.close()
                    store.close()

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

    async def test_influx_failure_retains_raw_then_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RawStore(Path(directory) / "server.sqlite3")
            store.commit_frame({"type": "frame", "car_id": "test-car", "seq": 1,
                                "timestamp_ns": 123456789, "can_id": 160,
                                "is_extended": False, "is_remote": False,
                                "is_fd": False, "is_error": False,
                                "dlc": 8, "data": "0000000000000000"})
            stop = asyncio.Event()
            with patch("telemetry.server.post_influx", side_effect=[RuntimeError("offline"), None]) as post:
                task = asyncio.create_task(export_forever(store, DBCDecoder([]), "http://unused", None, stop))
                try:
                    async def first_failure():
                        while post.call_count < 1:
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(first_failure(), 2)
                    self.assertEqual(store.status()["raw_frames"], 1)
                    self.assertEqual(store.status()["pending_influx"], 1)

                    async def exported():
                        while store.status()["pending_influx"] != 0:
                            await asyncio.sleep(0.05)
                    await asyncio.wait_for(exported(), 5)
                    self.assertEqual(post.call_count, 2)
                    self.assertEqual(store.status()["raw_frames"], 1)
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 3)
                    store.close()

    async def test_sender_resumes_after_receiver_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = CarSpool(Path(directory) / "car.sqlite3", "test-car")
            store = RawStore(Path(directory) / "server.sqlite3")
            spool.append(160, b"12345678")
            stop = asyncio.Event()
            # Bind a port, then close it so the sender starts during an outage.
            probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = probe.sockets[0].getsockname()[1]
            probe.close()
            await probe.wait_closed()
            sender = asyncio.create_task(send_forever(spool, "127.0.0.1", port, stop=stop))
            server = None
            try:
                await asyncio.sleep(0.15)
                self.assertEqual(spool.status()["queued"], 1)
                for _ in range(129):
                    spool.append(160, b"ABCDEFGH")
                server = await asyncio.start_server(
                    lambda r, w: serve_client(r, w, store, None), "127.0.0.1", port)
                async def delivered():
                    while store.status()["raw_frames"] != 130 or spool.status()["queued"] != 0:
                        await asyncio.sleep(0.05)
                await asyncio.wait_for(delivered(), 5)
                self.assertEqual(store.status()["streams"]["test-car"], 130)
                self.assertEqual(spool.status()["last_acked"], 130)
            finally:
                stop.set()
                await asyncio.wait_for(sender, 3)
                if server is not None:
                    server.close()
                    await server.wait_closed()
                spool.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
