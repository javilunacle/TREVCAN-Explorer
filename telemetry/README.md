# TREVCAN off-car telemetry

This is the runbook for the two-Pi vehicle telemetry system. Examples use
`~/TREVCAN-Telemetry`; existing checkout locations also work because service
installers use the actual checkout path. SocketCAN `can0` and `can1` must be
configured separately on the car Pi.

## Current layout

| Role | Address | Account | Purpose |
| --- | --- | --- | --- |
| Car Pi | `192.168.0.145` | `trevor` | Reads SocketCAN `can0` and `can1`, then streams raw frames |
| Telemetry Pi | `192.168.0.136` | `pi` | Receives/ACKs frames, decodes DBCs, runs InfluxDB, Grafana, and test controls |

```text
car can0/can1
  -> short RAM batch
  -> authenticated TCP batches
  -> telemetry-Pi RAM acceptance + ACK
  -> independent downstream workers:
       live: bus-aware DBC decoding -> small Grafana Live batches -> live viewers
       history: every raw/decoded sample + source-time transforms -> InfluxDB
                                                               -> historical viewers
```

The default policy is **discard disconnected data and resume fresh**. Neither Pi
opens a SQLite spool in the active pipeline. ACKs confirm acceptance into RAM,
not durable storage. Sequence gaps are expected and accepted after drops.

Both the car sender and historical-export queue are capped at 4096 frames and
one second of local queue age. The car discards frames while disconnected and
clears pending frames when the connection fails. It reconnects automatically
and sends newly captured samples; it never drains an old disk backlog.

Live and historical writes run independently. The Live worker normally sends
up to 256 fresh frames every 20 ms, preserving every decoded value in a batch.
During healthy capture the historical worker sends every raw frame and numeric
decoded update to InfluxDB with the original nanosecond capture timestamp.
An Influx failure discards pending history and samples collected during the
retry pause. Live continues independently. If Live fails, history can continue.

Live has a separate 8192-frame RAM cap and excludes samples more than two
seconds away from the telemetry Pi's clock. Keep both Pi clocks synchronized.
Receiver logs report live `sent`, `stale`, `overflow`, and `failed` counters,
plus `[history-buffer]` queue/drop counts. These are best-effort views: outages,
restart, or overload can leave gaps. A 20 ms batching interval is a target, not
an end-to-end latency guarantee.

This phase is supervised. The car sender, receiver, and TREVCAN Telemetry
Tools page are installed as user services but are **disabled at boot**.
Powering the car for unrelated work does not start telemetry. Boot automation
is deferred until the competition configuration is approved.

Use this system only on the team's trusted isolated LAN. TREVCAN Telemetry Tools
has no login: anyone connected to that LAN can start or stop telemetry. Its
restricted car key cannot open a shell or power off the Pi. The transport token
and web traffic are not encrypted yet.

## One-time installation

The latest `off_car` branch must be committed and pushed before pulling it onto
either Pi. Do not overwrite either Pi's local changes; check `git status -sb`
before switching or pulling.

### 1. Prepare the telemetry Pi

Connect to the telemetry Pi:

```bash
ssh pi@192.168.0.136
cd ~/TREVCAN-Telemetry
git status -sb
git fetch --all --prune
git switch off_car
git pull --ff-only
```

Create or update the Python environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create the private configuration only if it does not already exist:

```bash
test -f .env.telemetry || cp .env.telemetry.example .env.telemetry
nano .env.telemetry
chmod 600 .env.telemetry
```

Set every example value. These credentials have different purposes:

- `TELEMETRY_TOKEN`: shared only by the car sender and telemetry receiver.
- `INFLUXDB_TOKEN`: existing token with access to organization `docs` and
  bucket `home`. Preserve the working token when reusing the existing Docker
  volume.
- InfluxDB and Grafana admin usernames/passwords: web-service credentials.
- `GRAFANA_LIVE_URL='http://127.0.0.1:3000/api/live/push/trevcan'`: receiver's
  local Grafana push endpoint. Leave the Live token placeholder until Grafana
  has started, then create the publisher token as described below.
- `GRAFANA_BIND_ADDRESS='0.0.0.0'`: allows LAN viewers to reach Grafana.
- `TELEMETRY_CAR_HOST='192.168.0.145'` and
  `TELEMETRY_CAR_USER='trevor'`.

Never commit `.env.telemetry` or paste its tokens into chat or screenshots.

Render the current dashboards and start the database containers once so the
first button press does not have to download/start them:

```bash
set -a
source .env.telemetry
set +a
.venv/bin/python telemetry/grafana/render_provisioned.py
docker compose -f docker-compose.telemetry-pi.yml up -d
```

Open `http://telemetrypi.local:3000` (or `http://192.168.0.136:3000`) and sign in.
Under **Administration → Users and access → Service accounts**, create a
`trevcan-live` service account with the **Admin** role and generate a token.
Set `GRAFANA_LIVE_TOKEN` in the private `.env.telemetry` to that token. This is a
separate publisher credential; do not put it in dashboard JSON or on the car Pi.
The receiver launcher requires it before starting telemetry. Existing installs
need these two Live variables added to their existing private configuration.

Grafana accepts line protocol at this endpoint and serves subscribers over
WebSockets; no Telegraf service is required. See the official
[Grafana Live push tutorial](https://grafana.com/tutorials/stream-metrics-from-telegraf-to-grafana/).

Install the receiver service. Do not add `--enable-at-boot`:

```bash
bash telemetry/install_receiver_service.sh
systemctl --user is-enabled trevcan-telemetry-receiver.service
```

The expected result is `disabled`.

### 2. Prepare the car Pi

Create a dedicated telemetry checkout:

```bash
ssh trevor@192.168.0.145
git clone --branch off_car https://github.com/javilunacle/TREVCAN-Explorer.git \
  ~/TREVCAN-Telemetry
cd ~/TREVCAN-Telemetry
```

If that checkout already exists, update it instead:

```bash
cd ~/TREVCAN-Telemetry
git status -sb
git pull --ff-only
```

Create a local environment for the car sender:

```bash
python3 -m venv .venv
.venv/bin/pip install python-can
```

The installer uses this checkout's `.venv/bin/python`. Set
`TREVCAN_CAR_PYTHON` explicitly if another Python environment is required.

Install the car service, again without `--enable-at-boot`:

```bash
bash telemetry/install_car_service.sh 192.168.0.136
```

When prompted, enter the exact `TELEMETRY_TOKEN` from the telemetry Pi. The
installer copies only the small sender package to `~/.local/share/trevcan`,
stores its private configuration in `~/.config/trevcan/car.env`, and batches
frames in RAM. No spool file is created.

Verify that the service is installed but not running automatically:

```bash
systemctl --user is-enabled trevcan-telemetry-car.service
systemctl --user is-active trevcan-telemetry-car.service
```

Expected results are `disabled` and `inactive`.

### 3. Install restricted car control

Return to the telemetry Pi:

```bash
ssh pi@192.168.0.136
cd ~/TREVCAN-Telemetry
bash telemetry/setup_control_pi.sh 192.168.0.145 trevor
```

The script shows the scanned car-Pi SSH fingerprint. In a separate car-Pi
terminal, display the trusted fingerprint:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Approve the telemetry-Pi prompt only if the fingerprints match exactly. The
setup then asks for the normal car SSH password. It installs a dedicated key
that cannot open a shell, forward ports, run arbitrary commands, or power off
the Pi. It is restricted to telemetry status/start/stop. When upgrading from
the earlier prototype, it may ask for the car sudo password once to remove the
obsolete poweroff permission.

Install and start TREVCAN Telemetry Tools for the current boot:

```bash
bash telemetry/install_control_service.sh --start-now
systemctl --user status trevcan-telemetry-control.service --no-pager
```

The control service remains disabled at boot even when `--start-now` is used.

Install the friendly LAN address once:

```bash
bash telemetry/install_friendly_url_pi.sh
```

This sets the telemetry Pi hostname to `telemetrypi`, enables mDNS through
Avahi, and installs a small Nginx reverse proxy. It does not change the
telemetry data path. After the command completes, reconnecting SSH as
`pi@telemetrypi.local` also works. The numeric addresses remain available as
fallbacks if a viewer does not support `.local` name resolution.

## Running a vehicle test

After any telemetry-Pi reboot, a software lead performs this single preparation
step:

```bash
ssh pi@192.168.0.136
systemctl --user start trevcan-telemetry-control.service
```

Then the test operator does not need SSH:

1. Power the router, telemetry Pi, and car Pi.
2. Open `http://telemetrypi.local`. If `.local` resolution is unavailable on
   that viewer, use `http://192.168.0.136:8080`.
3. Select **Start Test**. This starts the receiver first, waits for TCP port
   8765, creates/reuses the current analysis session, and then starts the
   dual-bus car sender.
4. Select **Open Grafana dashboards** in Telemetry Tools, or open
   `http://telemetrypi.local:3000/dashboards` directly. All provisioned
   dashboards include a **Telemetry tools** link back to TREVCAN Telemetry
   Tools. Use
   **TREVCAN - Live Telemetry** for streamed speed, voltage, current, temperature,
   a DBC message selector, decoded samples, and recent raw CAN frames. This
   dashboard subscribes to Live channels and makes no InfluxDB queries. Its
   recent window begins with the subscription and is bounded in the browser;
   inspect capture timestamps if data stops. Use **TREVCAN - Historical Analysis**
   for distance, power/energy, rate of temperature change, transform diagnostics,
   and the alignment-gated GG plot. The other **Historical** dashboards query
   InfluxDB for recorded subsystem data. Normal
   dashboards automatically exclude synthetic scenario streams. Use the
   optional **Test session** selector only when reviewing an earlier run.
5. Select **End Test - Stop Telemetry** when capture should stop. This cleanly
   stops only the telemetry sender. The car Pi and every other car
   service remain powered.
6. Use the team's separate vehicle shutdown procedure if the car itself should
   be turned off. TREVCAN Telemetry Tools cannot power off the car Pi.

If the car is powered for some other reason, do not select **Start Test**. The
sender remains inactive.

`End Test` stops the car sender cleanly. The receiver may remain active on the
telemetry Pi so it can finish exporting accepted data. To stop the test-day
services manually afterward:

```bash
systemctl --user stop trevcan-telemetry-control.service
systemctl --user stop trevcan-telemetry-receiver.service
```

## Verification

Read running-process counters from the journals:

```bash
# Car Pi
journalctl --user -u trevcan-telemetry-car.service -f

# Telemetry Pi
journalctl --user -u trevcan-telemetry-receiver.service -f
sudo ss -ltnp | grep ':8765'
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

RAM queues have no separate `--status --db` command. Car logs contain
`[car-buffer]` with `connected`, `queued`, and `dropped`; receiver logs contain
`[history-buffer]` and `[grafana-live]`.

Expected behavior:

- `last_acked` and the in-memory receiver sequence increase during capture.
- The car `queued` count fluctuates near zero. A temporary rise is acceptable
  if it subsequently drains.
- `pending_influx` remains bounded and falls after input stops.
- Grafana timestamps remain close to current time.
- `[grafana-live]` reports increasing `sent` with zero `overflow` and `failed`
  during healthy capture. If `stale` rises, check clock synchronization and
  queue delay. `dropped` rises during outages/overload; this is intentional.
- Both `can0` and `can1` appear in the current data.
- The Historical Analysis dashboard begins a new set of distance and energy totals
  for each new supervised test session.

Service logs:

```bash
# Telemetry Pi
journalctl --user -u trevcan-telemetry-receiver.service -f
journalctl --user -u trevcan-telemetry-control.service -f

# Car Pi
journalctl --user -u trevcan-telemetry-car.service -f
```

## Data lifetime and outages

- Both Pis discard disconnected data. No capture data survives in RAM after
  its process restarts, and no SQLite queue is recovered on startup.
- Small buffers smooth normal batching only. They expire old entries and
  discard the oldest frames at capacity instead of growing during an outage.
- InfluxDB is the historical store. Successfully recorded samples remain for
  the bucket's 24-hour retention period; an outage appears as a time gap.
- Integrations and derivatives use source timestamps and their existing gap
  limits. Running transform state is held in RAM and resets when the receiver
  restarts. Begin a new test session after a receiver restart when comparing
  session totals. The session JSON file contains metadata, not a CAN backlog.
- Older SQLite files are left untouched and are never replayed automatically.
  The active pipeline has no archive migration or SQLite backfill tools.

After an outage or reboot, select **Start Test** as usual. Capture resumes with
current frames, without trying to recover disconnected data.

## Historical transform policy

The receiver reads `telemetry/transform_policies.json`. Calculations consume
every selected decoded update in source-timestamp order; the fast/medium/slow
policy changes only how often derived points are published to InfluxDB. It does
not replace the full-sample raw or decoded measurements. The initial publication targets
are 10 Hz for fast outputs, 5 Hz for medium outputs, and 1 Hz for slow outputs.
This work runs in the telemetry-Pi export worker after RAM acceptance;
it adds no transform processing to the car Pi.

The initial policy provides:

- mph-to-m/s conversion, distance integration, and a rolling speed derivative;
  decoded speeds at or below the configurable 0.1 mph stationary deadband are
  treated as zero for these derived calculations while raw decoded speed is
  preserved unchanged;
- pack power plus charge/discharge amp-hours and energy totals;
- maximum-accumulator-temperature slope and a diagnostic event for a 20 degC
  change within one second;
- CAN9 longitudinal/lateral acceleration and GG points only when `ImuValid` is
  set and `ImuAlign` reports a recent accepted alignment without errors.

Voltage and current messages do not need identical timestamps. The transform
uses the latest available pair when their timestamps are within 200 ms and
timestamps the result at the newer sample. Integrals use the actual elapsed
source time and stop across gaps larger than the policy limit rather than
inventing missing data.

The working HVC convention is **positive pack current = charging** and negative
pack current = discharging. It is explicit in the policy so it can be reversed
after the sign is verified against a known charge and traction test. Transform
warnings are diagnostic engineering aids, not safety alarms or interlocks.

## Synthetic end-to-end scenarios

Run synthetic scenarios on the **telemetry Pi**, with the real car test ended.
They use a unique `scenario-*` car ID and the normal authenticated TCP receiver,
DBC decoder, transform worker, InfluxDB, and Grafana path. They never open
SocketCAN and never transmit onto `can0` or `can1`. Normal vehicle dashboards
exclude their `scenario-*` stream IDs, while **TREVCAN - Historical Synthetic Validation**
shows only scenario output. **TREVCAN - Historical CAN Frames** is an
engineering view that can include both.

Start only the receiver, list the available scenarios, and run the full suite:

```bash
cd ~/TREVCAN-Telemetry
systemctl --user start trevcan-telemetry-receiver.service
bash telemetry/run_scenario_pi.sh --list
bash telemetry/run_scenario_pi.sh --scenario all
```

The wrapper reads both tokens from `.env.telemetry`, uses a RAM buffer,
waits for the receiver connection before capturing, and verifies the expected
InfluxDB output. Open **TREVCAN - Historical Analysis** with **Last 5 minutes** selected
to see only real-car derived results. Open **TREVCAN - Historical Synthetic Validation**
to see scenario calculations, GG points, diagnostic events, and synthetic HVC
fault bits without mixing them into vehicle graphs.

Synthetic Live output uses namespace `trevcan_synthetic` (or the configured
namespace plus `_synthetic`). The normal Live dashboard subscribes only to
`trevcan`, so synthetic output cannot appear as real-car Live samples.

Focused examples:

```bash
bash telemetry/run_scenario_pi.sh --scenario temperature-spike
bash telemetry/run_scenario_pi.sh --scenario gg-valid
bash telemetry/run_scenario_pi.sh --scenario gg-invalid
bash telemetry/run_scenario_pi.sh --scenario stale-electrical-pair
```

`temperature-spike` verifies the current diagnostic warning rule.
`gg-invalid` and `stale-electrical-pair` are negative tests: they pass only when
GG or pack-power output is correctly suppressed. The former `spool-drain`
scenario has been removed: disconnected samples are intentionally discarded.
Stop the receiver afterward if no real test is active:

```bash
systemctl --user stop trevcan-telemetry-receiver.service
```

## Current troubleshooting

### TREVCAN Telemetry Tools is unavailable

On the telemetry Pi:

```bash
systemctl --user restart trevcan-telemetry-control.service
journalctl --user -u trevcan-telemetry-control.service -n 100 --no-pager
```

### Sender reports an invalid token

The car's `~/.config/trevcan/car.env` and telemetry Pi's `.env.telemetry` must
contain the same `TELEMETRY_TOKEN`. Rerun the car installer to update its copy.
Do not confuse this transport token with `INFLUXDB_TOKEN`.

### Live dashboard is empty while history continues

Check `[grafana-live]` receiver logs and the private `GRAFANA_LIVE_TOKEN`.
The token needs the Grafana publisher service account's Admin role. Confirm
both Pi clocks, that fresh car frames are arriving, and that the viewer can
connect to Grafana's `/api/live/ws` endpoint. Direct access on port 3000 supports
WebSockets; any additional reverse proxy must forward WebSocket upgrades.
Historical data is still recorded during Live failures. Opening a Live panel
does not load earlier samples from InfluxDB; use a Historical dashboard for that.

### Samples are being dropped

Check the receiver service, TCP port 8765, LAN connectivity, Pi clocks, and
`[car-buffer]` / `[history-buffer]` / `[grafana-live]` counters. Queues are bounded;
a rising drop count means the link or consumer is unavailable or cannot keep
up. An outage leaves a gap instead of a disk backlog. Reducing Grafana polling
does not fix a transport problem.

### DBC decode warnings appear

Raw frames are still accepted and offered to the downstream workers. The current decoder routes these
DBCs by source bus:

- `can0`: BMS RTOS, HVC, VCU, inverter, and Baby MOBO DBCs.
- `can1`: DAQ, VCU, and `can9-database-01.09.dbc`.

Warnings about unexpected DLCs or multiplexor values indicate firmware/DBC
drift or an incomplete DBC definition. Fix the matching DBC or bus map; do not
change buffering merely to hide decode warnings.

### Existing car applications consume high CPU

The telemetry sender passively reads SocketCAN alongside other vehicle
applications. Do not disable unknown vehicle services without team approval.
Coordinate with their owners if combined CPU load or temperature affects testing.

## Updating after code changes

End the active test, then stop the receiver before upgrading. Reinstall the
car service: its old `--db` argument and SQLite configuration are no longer
used. Existing database files can remain in place. The installers remain
disabled-at-boot by default:

```bash
# Telemetry Pi
systemctl --user stop trevcan-telemetry-receiver.service
cd ~/TREVCAN-Telemetry
git pull --ff-only
bash telemetry/install_receiver_service.sh
bash telemetry/install_control_service.sh --start-now

# Car Pi
cd ~/TREVCAN-Telemetry
git pull --ff-only
bash telemetry/install_car_service.sh 192.168.0.136
```

Only use `--enable-at-boot` later, after competition startup behavior and
restart behavior have been deliberately validated.
