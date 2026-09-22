# Raw-CAN reliability prototype

This is a **separate path** from the existing FastAPI/React live explorer and
the lossy `StreamForwarder`. Run the car agent on the Pi and the receiver on
the laptop. The car agent does not load a DBC or contact InfluxDB.

```text
Pi:      SocketCAN or simulator -> bounded RAM queue -> SQLite spool -> TCP sender
Laptop:  TCP receiver -> SQLite raw frame commit -> ACK
                         -> DBC decoder -> InfluxDB -> Grafana
```

The receiver commits raw frames in SQLite with a unique `(car_id, seq)` key
before acknowledging each frame. The Pi only removes a spool row after its
matching ACK. If a connection dies after the server commit but before the ACK,
the Pi retransmits and the receiver recognizes the duplicate. The laptop's
InfluxDB exporter retries independently; an Influx outage leaves committed raw
frames in laptop SQLite with `influx_done=0`.

This is a lab prototype, not a production vehicle data logger. The TCP token
is sent in plaintext. Use an isolated trusted LAN for this test; add TLS or a
VPN, disk-capacity management, hardware throughput tests, and operational
monitoring before relying on it in a vehicle. A full RAM queue blocks the
capture loop, which can still lead to CAN kernel-buffer loss. Power failure
before the spool commit can also lose a frame. Neither is detectable solely
from transport sequence numbers.

## Prerequisites

- Python 3.11+ on both computers. Run commands from the repository root.
- `python-can` on the Pi for `--interface can0`; simulation uses only Python's
  standard library. The laptop needs `cantools` to decode a DBC.
- Docker Compose on the laptop if using the repository's InfluxDB/Grafana
  containers. The existing `docker-compose.yml` initializes InfluxDB with
  organization `docs` and bucket `home` and exposes InfluxDB on port 8086 and
  Grafana on port 3000. It does **not** provision Grafana's datasource or a
  dashboard. Change the Compose development credentials before using it beyond
  a private demo.

On the Pi, install `python-can` in a virtual environment. On the laptop,
install `cantools` in a virtual environment. The root `requirements.txt`
contains both packages if you prefer to install the full application.

## Laptop: receiver, InfluxDB, and Grafana

Start the database and visualization containers:

```powershell
docker compose up -d
```

Open InfluxDB at `http://localhost:8086`, sign in using the Compose setup
credentials, and obtain an API token that can write to `home`. The repository
contains an old token example in `temp.md`; do not reuse it. Rotate it if it
was ever active.

Set the tokens only in your current PowerShell session, then start the
receiver. Replace `YOUR-LAB-TOKEN` and `YOUR-INFLUX-TOKEN` with your values.

```powershell
$env:TELEMETRY_TOKEN = 'YOUR-LAB-TOKEN'
$env:INFLUXDB_TOKEN = 'YOUR-INFLUX-TOKEN'
python -m telemetry.server --host 0.0.0.0 --port 8765 --db telemetry-server.sqlite3 --dbc webserver/backend/dbc_files/master.dbc --influx-url 'http://127.0.0.1:8086/api/v2/write?org=docs&bucket=home&precision=ns'
```

`0.0.0.0` lets the Pi connect over your LAN. The receiver requires a shared
`TELEMETRY_TOKEN` when listening beyond localhost. Allow inbound TCP port
8765 in the laptop firewall on the private network if necessary. Only port
8765 needs to be reachable from the Pi; InfluxDB and Grafana remain on the
laptop. Use the laptop's LAN IP, not `localhost`, in the Pi command.

Check committed data in a second laptop terminal:

```powershell
python -m telemetry.server --status --db telemetry-server.sqlite3
```

`raw_frames` should grow. `pending_influx` should return to zero while InfluxDB
is reachable. The raw SQLite database remains the source of truth.

In Grafana at `http://localhost:3000`, add an InfluxDB datasource using Flux:
URL `http://influxdb2:8086`, organization `docs`, default bucket `home`, and
an InfluxDB token with read access. The URL is the Docker service name because
Grafana runs **inside** Docker. A simple time-series panel can use:

```flux
from(bucket: "home")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "can_signal")
  |> filter(fn: (r) => r.signal == "INV_Module_A_Temp" and r._field == "value")
```

The simulated frame uses ID `0xA0` (`INV_Temps_1`) in `master.dbc`, so this
signal should vary smoothly around 25 °C. Grafana's InfluxDB datasource
settings are documented at
<https://grafana.com/docs/grafana/latest/datasources/influxdb/configure/>.

## Pi: simulation first, then SocketCAN

On the Pi, from the same repository checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install python-can
export TELEMETRY_TOKEN='YOUR-LAB-TOKEN'
python -m telemetry.car --simulate-rate 20 --host LAPTOP_LAN_IP --port 8765 --db telemetry-car.sqlite3
```

Use the same lab token as on the laptop. The generated `car_id` is saved in
the Pi's spool and is reused on restart. **Do not delete either SQLite file
between outage/restart tests.**

After the simulation works, use real CAN frames:

```bash
ip -details link show can0
python -m telemetry.car --interface can0 --host LAPTOP_LAN_IP --port 8765 --db telemetry-car.sqlite3
```

Bring up and configure `can0` using your vehicle's correct CAN bitrate before
running the second command. The real capture path does not decode on the Pi.
The server decodes matching frames from the DBC supplied with `--dbc`.

Check the Pi spool from another terminal:

```bash
python -m telemetry.car --status --db telemetry-car.sqlite3
```

## Outage exercise

1. Start the laptop receiver, then the Pi simulation. Confirm `raw_frames`
   rises and `pending_influx` falls to zero.
2. Stop the laptop receiver with Ctrl+C, but leave the Pi agent running.
   The Pi's `queued` count should rise. The raw frames stay in its SQLite file.
3. Restart the laptop receiver with the **same** server database path. The Pi
   reconnects, retransmits from its oldest unacknowledged sequence, and its
   `queued` count should drain. The server's `(car_id, seq)` key prevents
   duplicates if an ACK was lost.
4. Separately, stop only InfluxDB while leaving the receiver running. The Pi
   should still receive ACKs; the laptop's `pending_influx` count rises.
   Restart InfluxDB and check that it falls to zero. Grafana catches up.

Run the local automated tests with:

```bash
python -m unittest telemetry.test_transport -v
```

The tests use temporary SQLite files and a localhost TCP receiver. They do
not prove Raspberry Pi CAN capture, throughput at your actual bus load,
power-failure behavior, Docker/InfluxDB compatibility, or Grafana display.
Those require the two-machine exercise above.

## Replay the old dashboard telemetry without a live car

The original `--simulate-rate 20` mode still generates only inverter
temperatures. For a broader **synthetic, read-only** demo, first generate a
raw-frame replay file on the laptop (where `cantools` and the DBCs are present):

```powershell
.\.venv\Scripts\python.exe -m telemetry.simulate --output telemetry-demo.jsonl
```

The generator selects 271 distinct telemetry messages from the currently
enabled BMS, HVC, inverter, master/VCU/DAQ, and MOBO DBCs, with two slightly
different samples per message. It omits command, request, reset, and ACK
messages, and resolves overlapping CAN IDs using the same priority as the
receiver. These are illustrative values, **not measured vehicle data**.

Restart the laptop receiver with all five DBCs in this exact order so the
replayed IDs decode as intended. Use the same `TELEMETRY_TOKEN` and
`INFLUXDB_TOKEN` variables as in the receiver example above:

```powershell
.\.venv\Scripts\python.exe -m telemetry.server --host 0.0.0.0 --port 8765 --db telemetry-server.sqlite3 --dbc webserver/backend/dbc_files/BMS-Firmware-RTOS-Complete.dbc --dbc webserver/backend/dbc_files/hvc.dbc --dbc webserver/backend/dbc_files/BMS-Inverter-Only.dbc --dbc webserver/backend/dbc_files/master.dbc --dbc webserver/backend/dbc_files/Baby_MOBO.dbc --influx-url 'http://127.0.0.1:8086/api/v2/write?org=docs&bucket=home&precision=ns'
```

For a laptop-only test, run the replay agent in another terminal:

```powershell
.\.venv\Scripts\python.exe -m telemetry.car --simulate-file telemetry-demo.jsonl --host 127.0.0.1 --port 8765 --db telemetry-demo-car.sqlite3
```

The replay defaults to 50 frames/second and loops continuously. The file
contains only raw IDs, bytes, and CAN flags; the replay agent does not need
`cantools`, load DBCs, use SocketCAN, or transmit onto the CAN bus. To move it
to a Pi, copy the generated JSONL file and the updated `telemetry/car.py` and
`telemetry/common.py` package files there, then run the same `--simulate-file`
command with `--host` set to the laptop's reachable LAN address. Keep the
receiver's DBC order unchanged. Use `--replay-rate` to lower the frame rate if
the Pi or network cannot keep up.

The Grafana Systems draft can now receive data for its existing BMS, HVC, VCU,
MOBO, and inverter panels. It is still a first-pass signal browser, not a
pixel-for-pixel replacement for the React dashboards. This simulation does
not validate real CAN wiring, DBC correctness on the actual car, or hardware
throughput. Do not run `--interface can0` for this replay exercise.
