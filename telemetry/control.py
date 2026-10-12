"""Small authenticated operator UI for supervised telemetry tests.

This process runs on the telemetry Pi. It starts the local receiver user unit
and invokes an allowlisted forced command on the car Pi over SSH. It never
accepts an arbitrary shell command from HTTP clients.
"""

import argparse
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>TREVCAN Test Controls</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #0b0d12; color: #f3f4f6; }
    main { max-width: 760px; margin: 0 auto; padding: 32px 18px; }
    h1 { margin-bottom: 4px; } .muted { color: #9ca3af; }
    .card { background: #171a21; border: 1px solid #303541; border-radius: 12px;
            padding: 18px; margin-top: 18px; }
    .status { font-size: 1.4rem; font-weight: 750; margin: 8px 0; }
    .ok { color: #65d46e; } .warn { color: #f4c95d; } .bad { color: #ff6868; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(200px,1fr)); gap: 12px; }
    button, input { border-radius: 8px; border: 1px solid #454b59; padding: 12px;
                    font: inherit; }
    input { width: 100%; box-sizing: border-box; background: #0f1218; color: white; }
    button { cursor: pointer; font-weight: 700; color: white; background: #2867d7; }
    button.secondary { background: #4b5563; }
    button.danger { background: #b4232f; }
    button:disabled { opacity: .45; cursor: wait; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; color: #d1d5db; }
  </style>
</head>
<body><main>
  <h1>TREVCAN Test Controls</h1>
  <div class="muted">Car Pi: trevor@192.168.0.145 | Telemetry Pi: 192.168.0.136</div>

  <section class="card">
    <label for="token">Operator token</label>
    <input id="token" type="password" autocomplete="current-password"
           placeholder="Enter the separate control token">
    <p class="muted">Stored only in this browser tab.</p>
  </section>

  <section class="card">
    <div class="muted">Current state</div>
    <div id="state" class="status warn">Enter the operator token</div>
    <pre id="details"></pre>
  </section>

  <section class="card grid">
    <button id="start">Start Test</button>
    <button id="stop" class="secondary">Stop Telemetry - Keep Car On</button>
    <button id="end" class="danger">End Test &amp; Safely Shut Down Car Pi</button>
  </section>

  <section class="card">
    <strong>End Test procedure</strong>
    <p class="muted">Wait until this page confirms that shutdown was requested. Then wait for
      the car Pi activity LED to stop before removing vehicle power.</p>
    <pre id="message"></pre>
  </section>
</main>
<script>
const token = document.getElementById('token');
const state = document.getElementById('state');
const details = document.getElementById('details');
const message = document.getElementById('message');
const buttons = [...document.querySelectorAll('button')];
token.value = sessionStorage.getItem('trevcanControlToken') || '';
token.addEventListener('input', () => {
  sessionStorage.setItem('trevcanControlToken', token.value);
  refresh();
});

async function call(path, method='GET') {
  if (!token.value) throw new Error('Enter the operator token first.');
  const response = await fetch(path, {
    method,
    headers: {'Authorization': `Bearer ${token.value}`, 'Content-Type': 'application/json'},
    body: method === 'POST' ? '{}' : undefined,
    cache: 'no-store'
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function show(data) {
  const running = data.car && data.car.state === 'running';
  state.textContent = running ? 'RUNNING' : (data.car?.state || 'OFFLINE').toUpperCase();
  state.className = `status ${running ? 'ok' : (data.car?.state === 'idle' ? 'warn' : 'bad')}`;
  details.textContent = JSON.stringify(data, null, 2);
}

async function refresh() {
  if (!token.value) return;
  try { show(await call('/api/status')); }
  catch (error) { state.textContent = error.message; state.className = 'status bad'; }
}

async function act(path, confirmation) {
  if (confirmation && !window.confirm(confirmation)) return;
  buttons.forEach(button => button.disabled = true);
  message.textContent = 'Working...';
  try {
    const data = await call(path, 'POST');
    message.textContent = data.message || JSON.stringify(data, null, 2);
    show(data.status || data);
  } catch (error) { message.textContent = error.message; }
  finally { buttons.forEach(button => button.disabled = false); setTimeout(refresh, 1000); }
}

document.getElementById('start').onclick = () => act('/api/start');
document.getElementById('stop').onclick = () => act('/api/stop',
  'Stop telemetry capture but keep the car Pi powered on?');
document.getElementById('end').onclick = () => act('/api/end',
  'End this test and shut down the car Pi? Wait for the safe-power indication before removing power.');
setInterval(refresh, 5000); refresh();
</script></body></html>"""


class ControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlConfig:
    car_host: str = "192.168.0.145"
    car_user: str = "trevor"
    ssh_identity: Path = Path.home() / ".config/trevcan/control_ed25519"
    known_hosts: Path = Path.home() / ".config/trevcan/control_known_hosts"
    receiver_unit: str = "trevcan-telemetry-receiver.service"
    receiver_port: int = 8765
    command_timeout: float = 60


class TestController:
    ACTIONS = frozenset({"status", "start", "stop", "safe-shutdown"})

    def __init__(self, config, *, runner=subprocess.run, connector=socket.create_connection):
        self.config = config
        self.runner = runner
        self.connector = connector
        self.action_lock = threading.Lock()

    def _run(self, argv, *, timeout=None, allow_failure=False):
        try:
            result = self.runner(
                argv, capture_output=True, text=True,
                timeout=timeout or self.config.command_timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError(str(exc)) from exc
        if result.returncode and not allow_failure:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise ControlError(detail)
        return result

    def _receiver_state(self):
        result = self._run(
            ["systemctl", "--user", "is-active", self.config.receiver_unit],
            timeout=10, allow_failure=True)
        return result.stdout.strip() or "unknown"

    def _start_receiver(self):
        self._run(["systemctl", "--user", "start", self.config.receiver_unit], timeout=45)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                connection = self.connector(("127.0.0.1", self.config.receiver_port), timeout=1)
                connection.close()
                return
            except OSError:
                time.sleep(0.25)
        raise ControlError("telemetry receiver did not open port 8765")

    def _remote(self, action):
        if action not in self.ACTIONS:
            raise ControlError("unsupported car action")
        target = f"{self.config.car_user}@{self.config.car_host}"
        argv = [
            "ssh", "-i", str(self.config.ssh_identity),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.config.known_hosts}",
            "-o", "ConnectTimeout=5",
            target, action,
        ]
        result = self._run(argv, allow_failure=(action == "safe-shutdown"))
        output = result.stdout.strip().splitlines()
        if output:
            try:
                return json.loads(output[-1])
            except json.JSONDecodeError as exc:
                raise ControlError(f"invalid car response: {output[-1]}") from exc
        if action == "safe-shutdown" and result.returncode == 255:
            return {"ok": True, "state": "shutting-down"}
        detail = result.stderr.strip() or f"car command exited {result.returncode}"
        raise ControlError(detail)

    def status(self):
        try:
            car = self._remote("status")
        except ControlError as exc:
            car = {"ok": False, "state": "offline", "error": str(exc)}
        return {"ok": True, "receiver": self._receiver_state(), "car": car}

    def start_test(self):
        with self.action_lock:
            self._start_receiver()
            self._remote("start")
            return {"message": "Test started; receiver and car capture are running.",
                    "status": self.status()}

    def stop_capture(self):
        with self.action_lock:
            self._remote("stop")
            return {"message": "Telemetry stopped; the car Pi remains powered.",
                    "status": self.status()}

    def end_test(self):
        with self.action_lock:
            response = self._remote("safe-shutdown")
            status = {"ok": True, "receiver": self._receiver_state(), "car": response}
            return {
                "message": (
                    "Car-Pi shutdown was requested. Wait for its activity LED to stop before "
                    "removing vehicle power."
                ),
                "status": status,
            }


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "TREVCAN-Control/1"

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline' 'self'; script-src 'unsafe-inline' 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.control_token}"
        return hmac.compare_digest(supplied, expected)

    def do_GET(self):
        if self.path == "/":
            self._send(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
            return
        if self.path != "/api/status":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid operator token"})
            return
        self._send(HTTPStatus.OK, self.server.controller.status())

    def do_POST(self):
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid operator token"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return
        self.rfile.read(length)
        actions = {
            "/api/start": self.server.controller.start_test,
            "/api/stop": self.server.controller.stop_capture,
            "/api/end": self.server.controller.end_test,
        }
        action = actions.get(self.path)
        if action is None:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            self._send(HTTPStatus.OK, action())
        except ControlError as exc:
            self._send(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def log_message(self, message, *args):
        print(f"[control] {self.address_string()} {message % args}", flush=True)


class ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller, token):
        super().__init__(address, ControlHandler)
        self.controller = controller
        self.control_token = token


def main():
    parser = argparse.ArgumentParser(description="TREVCAN supervised test control page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default=os.getenv("TELEMETRY_CONTROL_TOKEN"))
    parser.add_argument("--car-host", default=os.getenv("TELEMETRY_CAR_HOST", "192.168.0.145"))
    parser.add_argument("--car-user", default=os.getenv("TELEMETRY_CAR_USER", "trevor"))
    parser.add_argument("--identity", type=Path, default=Path(os.getenv(
        "TELEMETRY_CONTROL_IDENTITY", "~/.config/trevcan/control_ed25519")).expanduser())
    parser.add_argument("--known-hosts", type=Path, default=Path(os.getenv(
        "TELEMETRY_CONTROL_KNOWN_HOSTS", "~/.config/trevcan/control_known_hosts")).expanduser())
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        parser.error("TELEMETRY_CONTROL_TOKEN is required when listening beyond localhost")
    if not args.token:
        parser.error("an operator token is required")
    if len(args.token) < 24:
        parser.error("the operator token must contain at least 24 characters")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.car_host):
        parser.error("invalid car host")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", args.car_user):
        parser.error("invalid car user")
    if not shutil.which("ssh") or not shutil.which("systemctl"):
        parser.error("ssh and systemctl are required")
    if not args.identity.is_file():
        parser.error(f"missing SSH identity: {args.identity}")
    if not args.known_hosts.is_file():
        parser.error(f"missing SSH known-hosts file: {args.known_hosts}")

    config = ControlConfig(
        car_host=args.car_host, car_user=args.car_user,
        ssh_identity=args.identity, known_hosts=args.known_hosts)
    server = ControlHTTPServer((args.host, args.port), TestController(config), args.token)
    print(f"[control] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
