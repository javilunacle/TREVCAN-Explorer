import json
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from telemetry.control import (
    ControlConfig,
    ControlError,
    ControlHTTPServer,
    INDEX_HTML,
    TestController,
)


class _Connection:
    def close(self):
        pass


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        if argv[0] == "ssh":
            action = argv[-1]
            state = "running" if action in {"start", "status"} else "idle"
            output = json.dumps({"ok": True, "state": state}) + "\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class TestControllerTests(unittest.TestCase):
    def setUp(self):
        self.runner = _Runner()
        self.config = ControlConfig(
            car_host="192.168.0.145",
            car_user="trevor",
            ssh_identity=Path("/fixed/control-key"),
            known_hosts=Path("/fixed/known-hosts"),
        )
        self.controller = TestController(
            self.config, runner=self.runner,
            connector=lambda *args, **kwargs: _Connection(),
        )

    def test_remote_command_is_fixed_and_host_key_checking_is_strict(self):
        response = self.controller._remote("status")
        self.assertEqual(response["state"], "running")
        command = self.runner.calls[-1]
        self.assertEqual(command[-2:], ["trevor@192.168.0.145", "status"])
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        with self.assertRaises(ControlError):
            self.controller._remote("anything-else")
        with self.assertRaises(ControlError):
            self.controller._remote("safe-shutdown")

    def test_start_brings_up_receiver_before_car_sender(self):
        response = self.controller.start_test()
        calls = self.runner.calls
        receiver_start = calls.index([
            "systemctl", "--user", "start",
            "trevcan-telemetry-receiver.service",
        ])
        remote_start = next(
            index for index, command in enumerate(calls)
            if command[0] == "ssh" and command[-1] == "start"
        )
        self.assertLess(receiver_start, remote_start)
        self.assertEqual(response["status"]["car"]["state"], "running")

    def test_end_only_stops_telemetry_and_keeps_car_powered(self):
        ended = self.controller.end_test()
        remote_actions = [call[-1] for call in self.runner.calls if call[0] == "ssh"]
        self.assertIn("stop", remote_actions)
        self.assertNotIn("safe-shutdown", remote_actions)
        self.assertIn("remains powered", ended["message"])


class _HTTPController:
    def status(self):
        return {"ok": True, "receiver": "active", "car": {"state": "idle"}}

    def start_test(self):
        return {"message": "started", "status": self.status()}

    def stop_capture(self):
        return {"message": "stopped", "status": self.status()}

    def end_test(self):
        return {"message": "ended", "status": self.status()}


class ControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ControlHTTPServer(("127.0.0.1", 0), _HTTPController())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_status_is_available_without_a_token(self):
        with urllib.request.urlopen(self.base_url + "/api/status", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["car"]["state"], "idle")

    def test_start_is_available_on_the_local_network(self):
        request = urllib.request.Request(
            self.base_url + "/api/start",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["message"], "started")

    def test_cross_origin_action_is_rejected(self):
        request = urllib.request.Request(
            self.base_url + "/api/start",
            data=b"{}",
            method="POST",
            headers={"Origin": "https://unrelated.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        try:
            self.assertEqual(caught.exception.code, 403)
        finally:
            caught.exception.close()

    def test_index_describes_start_and_safe_process_stop(self):
        self.assertIn("TREVCAN Telemetry Tools", INDEX_HTML)
        self.assertIn("Start Test", INDEX_HTML)
        self.assertIn("End Test - Stop Telemetry", INDEX_HTML)
        self.assertIn("car Pi and", INDEX_HTML)
        self.assertIn("remain powered", INDEX_HTML)
        self.assertNotIn("Operator token", INDEX_HTML)
        self.assertNotIn("Safely Shut Down Car Pi", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
