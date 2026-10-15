"""Fresh-data reconnects, bounded RAM, and no runtime SQLite dependency."""

import asyncio
import unittest
from unittest.mock import patch

from .buffers import CarBuffer, RawBuffer
from .car import send_forever
from . import car as car_app
from .server import serve_client
from .test_live import eventually, frame


class BufferTests(unittest.TestCase):
    def test_disconnected_capture_is_discarded_and_reconnect_keeps_only_new_samples(self):
        car = CarBuffer("car")
        car.append(160, bytes(8))
        self.assertEqual(car.pending(), [])
        car.set_connected(True)
        car.append(160, bytes(8))
        self.assertEqual([item["seq"] for item in car.pending()], [2])
        car.set_connected(False)
        car.append(160, bytes(8))
        self.assertEqual(car.pending(), [])
        car.set_connected(True)
        car.append(160, bytes(8))
        self.assertEqual([item["seq"] for item in car.pending()], [4])
        self.assertEqual(car.status()["dropped"], 3)

    def test_overflow_and_age_bound_both_buffers_without_collapsing_signal_updates(self):
        for buffer in (CarBuffer("car", max_frames=2), RawBuffer(max_frames=2)):
            with self.subTest(buffer=type(buffer).__name__), patch(
                    "telemetry.buffers.time.monotonic", return_value=100):
                if isinstance(buffer, CarBuffer):
                    buffer.set_connected(True)
                    for _ in range(3):
                        buffer.append(160, bytes(8))
                else:
                    buffer.commit_batch([frame(seq) for seq in range(1, 4)])
                self.assertEqual([item["seq"] for item in buffer.pending()], [2, 3])
                self.assertEqual(buffer.dropped, 1)
            with patch("telemetry.buffers.time.monotonic", return_value=102):
                self.assertEqual(buffer.pending(), [])
                self.assertEqual(buffer.dropped, 3)

    def test_receiver_accepts_gaps_and_deduplicates_a_lost_ack(self):
        receiver = RawBuffer()
        receiver.commit_batch([frame(10), frame(15)])
        receiver.commit_batch([frame(10), frame(15), frame(27)])
        self.assertEqual([item["seq"] for item in receiver.pending()], [10, 15, 27])
        self.assertEqual(receiver.received, 3)

    def test_bad_batch_cannot_partially_enter_the_receiver(self):
        receiver = RawBuffer()
        with self.assertRaises(ValueError):
            receiver.commit_batch([frame(1), {**frame(2), "data": "00"}])
        self.assertEqual(receiver.pending(), [])
        self.assertEqual(receiver.last_sequence("test-car"), 0)

    def test_exporting_an_older_batch_does_not_remove_newly_accepted_samples(self):
        receiver = RawBuffer()
        receiver.commit_batch([frame(1), frame(2)])
        batch = receiver.pending()
        receiver.commit_frame(frame(3))
        receiver.finish_export(batch)
        self.assertEqual([item["seq"] for item in receiver.pending()], [3])

    def test_runtime_buffers_never_open_sqlite_and_restart_with_empty_state(self):
        with patch("sqlite3.connect", side_effect=AssertionError("SQLite is not a runtime dependency")):
            car = CarBuffer("vehicle")
            car.set_connected(True)
            car.append(160, bytes(8))
            receiver = RawBuffer()
            receiver.commit_batch(car.pending())
            receiver.finish_export(receiver.pending())
            replacement = CarBuffer("vehicle")
            self.assertNotEqual(car.car_id, replacement.car_id)
            self.assertEqual(RawBuffer().pending(), [])

    def test_default_car_cli_captures_and_sends_without_opening_sqlite(self):
        receiver = RawBuffer()
        actual_run = car_app.run

        async def run_briefly(args):
            server = await asyncio.start_server(
                lambda r, w: serve_client(r, w, receiver, None), "127.0.0.1", 0)
            args.port = server.sockets[0].getsockname()[1]
            task = asyncio.create_task(actual_run(args))
            try:
                await eventually(lambda: receiver.received >= 3 or task.done())
                if task.done():
                    await task  # Surface startup errors, including CLI argument mismatches.
                self.assertGreaterEqual(receiver.received, 3)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                server.close()
                await asyncio.wait_for(server.wait_closed(), 2)

        with (patch("sys.argv", ["telemetry.car", "--simulate-rate", "100"]),
              patch("telemetry.car.run", side_effect=run_briefly),
              patch("sqlite3.connect", side_effect=AssertionError("unexpected SQLite write"))):
            car_app.main()


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_outage_is_a_gap_and_receiver_resumes_with_current_data(self):
        car = CarBuffer("car")
        receiver = RawBuffer(max_age_seconds=10)
        clients = []

        async def accept(reader, writer):
            clients.append(writer)
            await serve_client(reader, writer, receiver, None)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        sender = asyncio.create_task(send_forever(car, "127.0.0.1", port, stop=stop))
        try:
            await eventually(lambda: car.connected)
            car.append(160, bytes(8))
            await eventually(lambda: car.last_acked == 1)
            server.close()
            clients[0].transport.abort()
            await asyncio.wait_for(server.wait_closed(), 2)
            await asyncio.sleep(0)
            car.append(160, bytes(8))  # Detect the broken link while sending.
            await eventually(lambda: not car.connected)
            car.append(160, bytes(8))  # Seq 3 is captured while disconnected.
            self.assertEqual(car.status()["queued"], 0)
            server = await asyncio.start_server(accept, "127.0.0.1", port)
            await eventually(lambda: car.connected)
            car.append(160, bytes(8))
            await eventually(lambda: car.last_acked == 4)
            sequences = [item["seq"] for item in receiver.pending()]
            self.assertIn(1, sequences)
            self.assertIn(4, sequences)
            self.assertNotIn(3, sequences)
            self.assertGreaterEqual(car.dropped, 1)
        finally:
            stop.set()
            await asyncio.wait_for(sender, 3)
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
