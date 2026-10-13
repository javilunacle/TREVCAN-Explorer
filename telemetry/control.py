"""Small LAN-only operator UI for supervised telemetry tests.

This process runs on the telemetry Pi. It starts the local receiver user unit
and invokes an allowlisted forced command on the car Pi over SSH. It never
accepts an arbitrary shell command from HTTP clients.
"""

import argparse
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
from urllib.parse import urlsplit

from .transforms import SessionRegistry


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>TREVCAN Telemetry Tools</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #000;
      color: #f5f5f5;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: #000; color: #f5f5f5; }
    .app-header {
      background: linear-gradient(135deg, #1e2538 0%, #16213e 100%);
      border-bottom: 2px solid rgba(102, 126, 234, .3);
      box-shadow: 0 4px 20px rgba(0, 0, 0, .4);
    }
    .header-content {
      max-width: 1080px; margin: 0 auto; padding: 16px 24px;
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
    }
    .brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
    .brand-icon {
      width: 48px; height: 48px; flex: 0 0 48px; border-radius: 12px;
      display: grid; place-items: center; color: #fff;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      box-shadow: 0 4px 14px rgba(102, 126, 234, .42);
    }
    .brand-icon svg { width: 29px; height: 29px; }
    h1 {
      margin: 0; font-size: 24px; line-height: 1.15; font-weight: 750;
      background: linear-gradient(135deg, #8093ff 0%, #aa7ad0 100%);
      -webkit-background-clip: text; background-clip: text; color: transparent;
    }
    .brand-subtitle { margin: 3px 0 0; color: #a0aec0; font-size: 13px; }
    .header-badge {
      padding: 6px 12px; border: 1px solid rgba(102, 126, 234, .35);
      border-radius: 6px; color: #91a0fa; background: rgba(102, 126, 234, .16);
      font-size: 11px; font-weight: 750; letter-spacing: .08em;
    }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .header-link {
      min-height: 36px; display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 12px; border: 1px solid #3b4368; border-radius: 7px;
      color: #f5f5f5; background: rgba(102, 126, 234, .14);
      text-decoration: none; font-size: 12px; font-weight: 750;
      transition: transform .16s ease, background .16s ease, border-color .16s ease;
    }
    .header-link:hover { transform: translateY(-1px); background: rgba(102, 126, 234, .25); border-color: #667eea; }
    .header-link:focus-visible { outline: 3px solid rgba(102, 126, 234, .6); outline-offset: 2px; }
    main { max-width: 1080px; margin: 0 auto; padding: 34px 24px 48px; }
    .eyebrow {
      margin: 0 0 7px; color: #ef4444; font-size: 11px; font-weight: 800;
      letter-spacing: .14em; text-transform: uppercase;
    }
    h2 { margin: 0; font-size: clamp(24px, 4vw, 34px); line-height: 1.15; }
    .intro { margin: 9px 0 24px; color: #999; max-width: 650px; line-height: 1.55; }
    .card {
      background: #141414; border: 1px solid #2a2a2a; border-radius: 12px;
      padding: 20px; box-shadow: 0 4px 20px rgba(0, 0, 0, .45);
    }
    .card + .card, .layout { margin-top: 18px; }
    .card-heading {
      display: flex; align-items: flex-start; justify-content: space-between;
      gap: 18px; padding-bottom: 16px; border-bottom: 1px solid #2a2a2a;
    }
    .section-label { color: #888; font-size: 12px; font-weight: 650; }
    .status-line { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
    .status-dot {
      width: 10px; height: 10px; flex: 0 0 10px; border-radius: 50%;
      background: #f59e0b; box-shadow: 0 0 0 4px rgba(245, 158, 11, .12);
    }
    .status-dot.ok { background: #4ade80; box-shadow: 0 0 0 4px rgba(74, 222, 128, .12); }
    .status-dot.bad { background: #ef4444; box-shadow: 0 0 0 4px rgba(239, 68, 68, .12); }
    .status { margin: 0; font-size: 22px; font-weight: 750; }
    .ok { color: #4ade80; } .warn { color: #f59e0b; } .bad { color: #ef4444; }
    .updated { color: #666; font-size: 12px; text-align: right; }
    .status-grid {
      display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px; margin-top: 16px;
    }
    .metric { min-height: 90px; padding: 14px; background: #0a0a0a; border: 1px solid #222; border-radius: 9px; }
    .metric-label { color: #777; font-size: 11px; font-weight: 750; letter-spacing: .06em; text-transform: uppercase; }
    .metric-value { margin-top: 8px; color: #f5f5f5; font-size: 17px; font-weight: 650; overflow-wrap: anywhere; }
    .metric-note { margin-top: 4px; color: #666; font-size: 12px; }
    details { margin-top: 16px; }
    summary { color: #a0aec0; cursor: pointer; font-size: 13px; }
    pre {
      margin: 12px 0 0; padding: 14px; white-space: pre-wrap; overflow-wrap: anywhere;
      border: 1px solid #222; border-radius: 8px; background: #080808; color: #b5b5b5;
      font: 12px/1.55 Consolas, Monaco, "Courier New", monospace;
    }
    .layout { display: grid; grid-template-columns: 1.15fr .85fr; gap: 18px; }
    h3 { margin: 0; font-size: 18px; }
    .card-copy { margin: 7px 0 17px; color: #888; font-size: 14px; line-height: 1.5; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    button {
      min-height: 48px; border: 0; border-radius: 8px; padding: 12px 16px;
      font: inherit; cursor: pointer; font-weight: 700; color: #fff;
      transition: transform .16s ease, box-shadow .16s ease, background .16s ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:focus-visible { outline: 3px solid rgba(102, 126, 234, .6); outline-offset: 2px; }
    button.primary {
      background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%);
      box-shadow: 0 3px 12px rgba(220, 38, 38, .25);
    }
    button.primary:hover:not(:disabled) { background: linear-gradient(135deg, #ef4444, #b91c1c); }
    button.secondary { background: #1a1a1a; border: 1px solid #353535; color: #f5f5f5; }
    button.secondary:hover:not(:disabled) { background: #252525; border-color: #dc2626; }
    button:disabled { opacity: .45; cursor: wait; transform: none; }
    .safety {
      border-left: 3px solid #667eea; background: linear-gradient(90deg, rgba(102, 126, 234, .09), transparent 60%);
    }
    .safety p { margin: 10px 0 0; color: #a0a0a0; font-size: 14px; line-height: 1.55; }
    #message:empty { display: none; }
    #message { margin-top: 16px; color: #d1d5db; }
    @media (max-width: 720px) {
      .header-content, main { padding-left: 16px; padding-right: 16px; }
      .header-badge { display: none; }
      .header-link { padding: 8px 10px; }
      .header-link-label { display: none; }
      .brand { gap: 11px; }
      .brand-icon { width: 38px; height: 38px; flex-basis: 38px; border-radius: 9px; }
      .brand-icon svg { width: 23px; height: 23px; }
      h1 { font-size: 18px; }
      .brand-subtitle { font-size: 11px; }
      .status-grid, .layout, .actions { grid-template-columns: 1fr; }
      .card-heading { align-items: center; }
    }
  </style>
</head>
<body>
<header class="app-header">
  <div class="header-content">
    <div class="brand">
      <div class="brand-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 12h4l2.2-7 4.1 14 2.2-7H21"/>
        </svg>
      </div>
      <div>
        <h1>TREVCAN Telemetry Tools</h1>
        <p class="brand-subtitle">Vehicle telemetry test control</p>
      </div>
    </div>
    <div class="header-actions">
      <a id="grafana-link" class="header-link" href="http://telemetrypi.local:3000/dashboards" target="_blank" rel="noopener">
        <span aria-hidden="true">&#8599;</span><span class="header-link-label">Open Grafana dashboards</span><span class="header-link-label" aria-hidden="true">&#8594;</span>
      </a>
      <div class="header-badge">TELEMETRY</div>
    </div>
  </div>
</header>
<main>
  <p class="eyebrow">Test session</p>
  <h2>Off-car telemetry</h2>
  <p class="intro">Start and monitor the car-to-pit telemetry pipeline from one place. These controls affect telemetry capture only.</p>

  <section class="card" aria-labelledby="system-status-label">
    <div class="card-heading">
      <div>
        <div id="system-status-label" class="section-label">SYSTEM STATUS</div>
        <div class="status-line">
          <span id="state-dot" class="status-dot"></span>
          <div id="state" class="status warn" aria-live="polite">Checking...</div>
        </div>
      </div>
      <div id="updated" class="updated">Contacting telemetry services</div>
    </div>
    <div class="status-grid">
      <div class="metric">
        <div class="metric-label">Telemetry receiver</div>
        <div id="receiver" class="metric-value">Checking...</div>
        <div class="metric-note">Telemetry Pi · 192.168.0.136</div>
      </div>
      <div class="metric">
        <div class="metric-label">Car sender</div>
        <div id="sender" class="metric-value">Checking...</div>
        <div class="metric-note">Car Pi · 192.168.0.145</div>
      </div>
      <div class="metric">
        <div class="metric-label">Startup mode</div>
        <div id="startup" class="metric-value">Checking...</div>
        <div class="metric-note">Manual supervised testing</div>
      </div>
    </div>
    <details>
      <summary>System details</summary>
      <pre id="details"></pre>
    </details>
  </section>

  <div class="layout">
    <section class="card">
      <h3>Test controls</h3>
      <p class="card-copy">Start the receiver and dual-bus sender, or end telemetry capture cleanly.</p>
      <div class="actions">
        <button id="start" class="primary">Start Test</button>
        <button id="end" class="secondary">End Test - Stop Telemetry</button>
      </div>
      <pre id="message" aria-live="polite"></pre>
    </section>

    <section class="card safety">
      <h3>Safe end-of-test behavior</h3>
      <p>End Test stops and syncs the telemetry sender only. The car Pi and every other vehicle service remain powered.</p>
      <p>Use the team's separate vehicle shutdown procedure when the car itself should be turned off.</p>
    </section>
  </div>
</main>
<script>
const state = document.getElementById('state');
const stateDot = document.getElementById('state-dot');
const details = document.getElementById('details');
const message = document.getElementById('message');
const receiver = document.getElementById('receiver');
const sender = document.getElementById('sender');
const startup = document.getElementById('startup');
const updated = document.getElementById('updated');
const buttons = [...document.querySelectorAll('button')];
const grafanaLink = document.getElementById('grafana-link');
const grafanaUrl = new URL(window.location.href);
grafanaUrl.port = '3000';
grafanaUrl.pathname = '/dashboards';
grafanaUrl.search = '';
grafanaUrl.hash = '';
grafanaLink.href = grafanaUrl.toString();
async function call(path, method='GET') {
  const response = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: method === 'POST' ? '{}' : undefined,
    cache: 'no-store'
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function show(data) {
  const carState = data.car?.state || 'offline';
  const receiverState = data.receiver || 'unknown';
  const senderRunning = carState === 'running';
  const pipelineRunning = senderRunning && receiverState === 'active';
  const ready = carState === 'idle';
  state.textContent = pipelineRunning ? 'Telemetry running' :
    (ready ? 'Ready for test' : (senderRunning ? 'Receiver unavailable' : 'Telemetry unavailable'));
  state.className = `status ${pipelineRunning ? 'ok' : (ready ? 'warn' : 'bad')}`;
  stateDot.className = `status-dot ${pipelineRunning ? 'ok' : (ready ? '' : 'bad')}`;
  receiver.textContent = receiverState === 'active' ? 'Active' : receiverState;
  receiver.className = `metric-value ${receiverState === 'active' ? 'ok' : 'warn'}`;
  sender.textContent = carState.charAt(0).toUpperCase() + carState.slice(1);
  sender.className = `metric-value ${senderRunning ? 'ok' : (carState === 'idle' ? 'warn' : 'bad')}`;
  const bootMode = data.car?.boot_enabled || 'unknown';
  startup.textContent = bootMode === 'disabled' ? 'Manual' : bootMode;
  updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  details.textContent = JSON.stringify(data, null, 2);
}

async function refresh() {
  try { show(await call('/api/status')); }
  catch (error) {
    state.textContent = 'Status unavailable'; state.className = 'status bad';
    stateDot.className = 'status-dot bad'; details.textContent = error.message;
    updated.textContent = 'Unable to refresh';
  }
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
document.getElementById('end').onclick = () => act('/api/end',
  'End this test and stop telemetry capture? The car Pi will remain powered on.');
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
    session_file: Path = Path.home() / "trevcan-data/telemetry-sessions.json"


class TestController:
    ACTIONS = frozenset({"status", "start", "stop"})

    def __init__(self, config, *, runner=subprocess.run, connector=socket.create_connection,
                 session_registry=None):
        self.config = config
        self.runner = runner
        self.connector = connector
        self.sessions = session_registry or SessionRegistry(config.session_file)
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
        result = self._run(argv)
        output = result.stdout.strip().splitlines()
        if output:
            try:
                return json.loads(output[-1])
            except json.JSONDecodeError as exc:
                raise ControlError(f"invalid car response: {output[-1]}") from exc
        detail = result.stderr.strip() or f"car command exited {result.returncode}"
        raise ControlError(detail)

    def status(self):
        try:
            car = self._remote("status")
        except ControlError as exc:
            car = {"ok": False, "state": "offline", "error": str(exc)}
        return {"ok": True, "receiver": self._receiver_state(), "car": car,
                "session": self.sessions.active()}

    def start_test(self):
        with self.action_lock:
            try:
                car_before = self._remote("status")
            except ControlError:
                car_before = {"state": "offline"}
            previous = self.sessions.active()
            session = self.sessions.start(
                force_new=car_before.get("state") != "running")
            created = previous is None or previous.get("id") != session["id"]
            try:
                self._start_receiver()
                self._remote("start")
            except Exception:
                if created:
                    self.sessions.end()
                raise
            return {"message": "Test started; receiver and car capture are running.",
                    "session": session, "status": self.status()}

    def stop_capture(self):
        with self.action_lock:
            self._remote("stop")
            session = self.sessions.end()
            return {"message": "Test ended and telemetry stopped; the car Pi remains powered.",
                    "session": session, "status": self.status()}

    def end_test(self):
        return self.stop_capture()


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

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")

    def do_GET(self):
        if self.path == "/":
            self._send(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
            return
        if self.path != "/api/status":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send(HTTPStatus.OK, self.server.controller.status())

    def do_POST(self):
        if not self._same_origin():
            self._send(HTTPStatus.FORBIDDEN, {"error": "cross-origin action rejected"})
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

    def __init__(self, address, controller):
        super().__init__(address, ControlHandler)
        self.controller = controller


def main():
    parser = argparse.ArgumentParser(description="TREVCAN supervised test control page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--car-host", default=os.getenv("TELEMETRY_CAR_HOST", "192.168.0.145"))
    parser.add_argument("--car-user", default=os.getenv("TELEMETRY_CAR_USER", "trevor"))
    parser.add_argument("--identity", type=Path, default=Path(os.getenv(
        "TELEMETRY_CONTROL_IDENTITY", "~/.config/trevcan/control_ed25519")).expanduser())
    parser.add_argument("--known-hosts", type=Path, default=Path(os.getenv(
        "TELEMETRY_CONTROL_KNOWN_HOSTS", "~/.config/trevcan/control_known_hosts")).expanduser())
    parser.add_argument("--session-file", type=Path, default=Path(os.getenv(
        "TREVCAN_SESSION_FILE", "~/trevcan-data/telemetry-sessions.json")).expanduser())
    args = parser.parse_args()

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
        ssh_identity=args.identity, known_hosts=args.known_hosts,
        session_file=args.session_file)
    server = ControlHTTPServer((args.host, args.port), TestController(config))
    print(f"[control] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
