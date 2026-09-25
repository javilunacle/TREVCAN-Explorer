# Raw-CAN reliability prototype

This is a **separate path** from the existing FastAPI/React live explorer and
the lossy `StreamForwarder`. Run the car agent on the Pi and the receiver on
the laptop. The car agent does not load a DBC or contact InfluxDB.

```text
Pi:      SocketCAN bus(es) -> bounded RAM queue -> batched SQLite spool -> TCP batches
Laptop:  TCP receiver -> batched SQLite raw commit -> cumulative durable ACK
                         -> DBC decoder -> InfluxDB -> Grafana
```

The receiver commits raw frames in SQLite with a unique `(car_id, seq)` key
before acknowledging the committed sequence range. The Pi only removes spool
rows through the cumulative durable ACK. If a connection dies after the server
commit but before the ACK, the Pi retransmits and the receiver recognizes the duplicate. The laptop's
InfluxDB exporter retries independently; an Influx outage leaves committed raw
frames in laptop SQLite with `influx_done=0`.

This is a lab prototype, not a production vehicle data logger. The TCP token
is sent in plaintext. Use an isolated trusted LAN for this test; add TLS or a
VPN, disk-capacity management, hardware throughput tests, and operational
monitoring before relying on it in a vehicle. A full RAM queue blocks the
capture loop, which can still lead to CAN kernel-buffer loss. Power failure
before the next short spool batch commits can also lose those RAM-queued frames. Neither is detectable solely
from transport sequence numbers.

## Prerequisites

- Python 3.11+ on both computers. Run commands from the repository root.
- `python-can` on the Pi for `--interface can0`; repeat `--interface` to capture
  more than one SocketCAN bus. Simulation uses only Python's standard library.
  The laptop needs `cantools` to decode a DBC.
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
python -m telemetry.car --interface can0 --interface can1 --host LAPTOP_LAN_IP --port 8765 --db telemetry-car.sqlite3
```

Bring up and configure `can0` using your vehicle's correct CAN bitrate before
running the second command. The real capture path does not decode on the Pi.
Both interfaces share one car identity and global sequence, while each frame
retains its `can0` or `can1` origin in SQLite, TCP, InfluxDB, and Grafana. The
server decodes matching frames from the DBCs supplied with `--dbc`.

Check the Pi spool from another terminal. Status opens SQLite read-only and
does not compete for the single-writer lock:

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

## Telemetry Pi startup

The Pi-specific stack keeps InfluxDB private on loopback, exposes Grafana to
the LAN on port 3000, persists all three databases in Docker volumes or the
host data directory, and provisions the current datasource and dashboard
drafts automatically.

On the telemetry Pi, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install cantools
cp .env.telemetry.example .env.telemetry
nano .env.telemetry
chmod 600 .env.telemetry
bash telemetry/start_server_pi.sh
```

Replace every example secret in `.env.telemetry`. `TELEMETRY_TOKEN` must be
the same shared value used by the car Pi. `INFLUXDB_TOKEN` should be a separate,
long random value. The two admin passwords protect the web applications.
Never commit `.env.telemetry`; it is ignored by Git.

The startup script renders provisioning-compatible copies of the two Grafana
drafts, starts the services in `docker-compose.telemetry-pi.yml`, and then runs
the durable receiver in the foreground. Stop the receiver with Ctrl+C; Docker
services remain running. Restart it with the same script and data paths.

For the current LAN layout, the car Pi sends to `192.168.0.110:8765`, and
viewers open `http://192.168.0.110:3000`. The server archive defaults to
`~/trevcan-data/telemetry-server.sqlite3`. Check it from another SSH session:

```bash
.venv/bin/python -m telemetry.server --status \
  --db ~/trevcan-data/telemetry-server.sqlite3
```

This is a manual foreground startup script, not yet a systemd boot service.
Run and validate the complete two-Pi outage test before enabling automatic
startup on a vehicle.

## Car Pi startup

The current two-Pi demo uses these LAN assignments. Reserve both addresses in
the router so DHCP does not change them:

| Role | Model | Hostname | Username | LAN address |
| --- | --- | --- | --- | --- |
| Raw-frame sender | Pi 3 | `trevcan-pi` | `pi4` | `192.168.0.100` |
| Telemetry server | Pi 4 | `telemetry-pi` | `pi` | `192.168.0.110` |

The car Pi needs only Python, the `telemetry` package files, and the generated
raw replay file. It does not need Docker, InfluxDB, Grafana, `cantools`, or any
DBC files. For the current manually copied layout, confirm these exist:

```text
/home/pi4/telemetry/__init__.py
/home/pi4/telemetry/common.py
/home/pi4/telemetry/car.py
/home/pi4/telemetry-demo.jsonl
```

Wait until the telemetry Pi prints
`[server] listening on 0.0.0.0:8765`. Then connect to the car Pi:

```powershell
ssh pi4@192.168.0.100
```

Enter the exact same shared `TELEMETRY_TOKEN` configured in the telemetry Pi's
`.env.telemetry` file. This is not the InfluxDB token:

```bash
read -rsp 'Shared Pi3-Pi4 token: ' TELEMETRY_TOKEN
echo
export TELEMETRY_TOKEN
```

Start the full synthetic raw-frame replay:

```bash
cd ~
python3 -m telemetry.car \
  --simulate-file ~/telemetry-demo.jsonl \
  --replay-rate 50 \
  --host 192.168.0.110 \
  --port 8765 \
  --db ~/telemetry-all-signals.sqlite3
```

The expected startup includes `[car]` and `[simulation]` messages followed by
periodic `[spool]` status. A small fluctuating `queued` value is normal while
new frames are being captured and older frames are acknowledged. Check the
spool from a second SSH session without stopping the sender:

```bash
cd ~
python3 -m telemetry.car --status \
  --db ~/telemetry-all-signals.sqlite3
```

`--replay-rate` is the **total aggregate frame rate**, not a rate per message.
The initial value of 50 frames/second is a safe functional smoke test. Because
the replay rotates through 271 distinct message types, each type appears only
about once every 5.4 seconds at that setting; this is not a realistic vehicle
cadence.

Do not infer a full-bus rate from the current DBC files. Only 24 of the 271
selected telemetry messages declare a cycle time. Those 24 definitions alone
sum to approximately 1,531 frames/second, while the other 247 messages have no
declared rate. Measure the actual car bus or obtain an approved message-rate
table before choosing the final target. A uniform replay also cannot reproduce
the true mix of fast and slow messages; exact timing requires replaying a
timestamped capture or adding a per-message scheduler.

For throughput testing, increase the aggregate rate in stages (for example,
50, 250, 500, 1000, and then the measured target). At each stage, verify that
the Pi 3 `queued` count does not trend upward indefinitely and that the Pi 4
`pending_influx` count remains bounded and recovers to zero after input stops.
This establishes the sustainable end-to-end rate on the actual hardware; the
unit tests do not establish that limit.

If the server or network disappears, the sender prints `[sender] disconnected`,
continues adding frames to the same SQLite spool, and retries the connection.
After the Pi 4 returns, `queued` should drain and `last_acked` should advance.
Do not delete the spool between outage and reconnect tests. Stop the foreground
sender with Ctrl+C.

View the provisioned dashboards from another device on the same non-isolated
LAN at `http://192.168.0.110:3000`. No internet port forwarding is required.
The `TREVCAN` folder contains separate Vehicle Overview, BMS, HVC, Inverter,
VCU, and MOBO dashboards plus the combined raw/decoded CAN Explorer. Grafana
is read-only with respect to the vehicle: React controls that reset devices,
change configuration, or transmit CAN frames are intentionally not reproduced.

## Real car: CANable and classical CAN

Treat this as a supervised, stationary vehicle test and follow the team's
electrical/tractive-system safety procedure. The telemetry capture process is a
passive SocketCAN reader, but do not stop unknown vehicle services or change a
CAN bitrate without identifying their owners and getting approval.

CANable is one possible physical USB-to-CAN adapter. The tested car currently
exposes onboard MCP251x SPI controllers instead. In either case, SocketCAN is
the Linux kernel interface exposed by the driver. The sender reads that
interface; it does not access the adapter directly and it does not transmit frames. Classical
CAN normally appears with an MTU of 16 rather than the CAN-FD MTU of 72.

Before changing services, inspect the car Pi:

```bash
hostname
hostname -I
uname -m
python3 --version
systemctl is-active trevcan-explorer.service || true
systemctl --type=service --state=running --no-pager
ip -brief link show type can
ip -details -statistics link show can0
readlink -f /sys/class/net/can0/device/driver || true
```

If `can0` is already `UP`, shows the team-approved classical-CAN bitrate, and
the old Explorer receives frames from it, do not take it down or reconfigure
it. SocketCAN supports multiple passive listeners, so the Explorer and reliable
sender can usually read concurrently. First verify reception without writing:

```bash
candump -L -n 20 can0
```

If `candump` is unavailable, install the distribution's `can-utils` package
only with the team administrator's approval. Do not use `cansend`, enable test
modes, or start any replay process on the live vehicle bus.

The old Explorer only needs to be stopped if the service owner confirms it is
safe and it is reconfiguring the interface, transmitting unwanted test traffic,
duplicating the old Influx forwarding path, or consuming too many resources.
Inspect its status and logs first:

```bash
sudo systemctl status trevcan-explorer.service --no-pager
sudo journalctl -u trevcan-explorer.service -n 100 --no-pager
```

If an approved test requires stopping that specific service, record whether it
was active, stop only that named service, and restore it after the test:

```bash
sudo systemctl stop trevcan-explorer.service
# run the capture test
sudo systemctl start trevcan-explorer.service
```

Do not stop other dashboard, logging, router, safety, or CAN-interface services
merely because they are running.

### Install the isolated raw sender

The reliable sender needs `telemetry/__init__.py`, `telemetry/common.py`, and
`telemetry/car.py`; it does not need a DBC. Keeping those files in the car
user's home directory avoids replacing the existing Explorer checkout. From
the laptop repository, substitute the actual car username and IP:

```powershell
ssh CAR_USER@CAR_PI_IP "mkdir -p ~/telemetry"
scp .\telemetry\__init__.py .\telemetry\common.py .\telemetry\car.py CAR_USER@CAR_PI_IP:~/telemetry/
```

On the car Pi, use a small virtual environment so the old application remains
untouched:

```bash
python3 -m venv ~/trevcan-telemetry-venv
~/trevcan-telemetry-venv/bin/pip install python-can
```

Compare `date -Is` on the car and telemetry Pis before capture. Their clocks
must agree because the car timestamp becomes the InfluxDB/Grafana timestamp.
On an isolated LAN where NTP is active but unsynchronized, a temporary manual
sync from the car Pi is:

```bash
sudo date -s "$(ssh pi@192.168.0.110 'date -Is')"
```

Use the same shared `TELEMETRY_TOKEN` as the telemetry Pi, but never use or copy
the InfluxDB token onto the car. Start the telemetry-Pi receiver first. Then on
the car Pi:

```bash
cd ~
read -rsp 'Shared car-telemetry token: ' TELEMETRY_TOKEN
echo
export TELEMETRY_TOKEN

~/trevcan-telemetry-venv/bin/python -m telemetry.car \
  --interface can0 \
  --interface can1 \
  --host 192.168.0.110 \
  --port 8765 \
  --db ~/telemetry-real-can.sqlite3
```

Use a new spool filename for the first pairing with a new/empty server archive.
The spool's car ID and sequence history must match the durable server history;
reusing an advanced spool against an empty server is correctly rejected as a
sequence gap. Do not delete either database during an outage/reconnect test.

Verify the car status from a second SSH session:

```bash
~/trevcan-telemetry-venv/bin/python -m telemetry.car --status \
  --db ~/telemetry-real-can.sqlite3
```

Verify the telemetry Pi from another session:

```bash
cd ~/TREVCAN-Explorer
.venv/bin/python -m telemetry.server --status \
  --db ~/trevcan-data/telemetry-server.sqlite3
```

The initial pass proves live capture only when raw counts and acknowledged
sequences advance, `pending_influx` returns to zero, and Grafana shows current
timestamps. Then perform the controlled outage test: disconnect only the
sender-to-server network path, confirm the car `queued` count rises, reconnect,
and confirm it drains without sequence gaps.

Upgrade and restart the telemetry receiver before starting an upgraded car
sender: the batched wire message and cumulative ACK protocol require both ends
to use the same revision. One process may capture `can0` and `can1`, but never
start two processes against the same spool file; the car agent rejects the
second writer instead of allowing sequence corruption.

The defaults commit at most 256 frames or 20 ms of capture in one car SQLite
transaction, send at most 256 frames per TCP batch, commit the receiver batch in
one `synchronous=FULL` transaction, and delete the acknowledged range in one
car transaction. This retains disconnect/restart recovery without forcing
three durable disk transactions for every frame. `--spool-batch-size`,
`--spool-flush-ms`, and `--window-size` are available for measured tuning; do
not claim a zero-loss rate until the real two-bus load and kernel drop counters
have been tested.

Do not run synthetic replay and real SocketCAN capture with the same spool
or at the same time. Real SocketCAN capture, sustained vehicle-bus throughput,
CANable/kernel buffer loss, power-loss behavior, and dashboard correctness are
hardware tests; synthetic replay does not prove them.
