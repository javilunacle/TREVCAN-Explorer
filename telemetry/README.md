# TREVCAN off-car telemetry

This is the current runbook for the two-Pi vehicle telemetry system.

## Current layout

| Role | Address | Account | Purpose |
| --- | --- | --- | --- |
| Car Pi | `192.168.0.145` | `trevor` | Reads SocketCAN `can0` and `can1`, then durably sends raw frames |
| Telemetry Pi | `192.168.0.136` | `pi` | Receives/ACKs frames, decodes DBCs, runs InfluxDB, Grafana, and test controls |

```text
car can0/can1
  -> short RAM batch
  -> car SQLite spool
  -> authenticated TCP batches
  -> telemetry-Pi SQLite durable commit
  -> cumulative ACK to car
  -> bus-aware DBC decoding and 100 ms visualization snapshots
  -> InfluxDB
  -> Grafana viewers
```

The car removes a frame from its spool only after the telemetry Pi has durably
committed and acknowledged it. A network or receiver outage therefore grows
the car queue; reconnecting drains it. Decode failures do not prevent raw
storage or ACKs.

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
cd ~/TREVCAN-Explorer
git status -sb
git fetch --all --prune
git switch off_car
git pull --ff-only
```

Create or update the Python environment:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip cantools
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

Install the receiver service. Do not add `--enable-at-boot`:

```bash
bash telemetry/install_receiver_service.sh
systemctl --user is-enabled trevcan-telemetry-receiver.service
```

The expected result is `disabled`.

### 2. Prepare the car Pi

Use a separate telemetry checkout so the existing in-car Explorer checkout is
not replaced:

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

Confirm the existing vehicle Python environment still provides `python-can`:

```bash
/home/trevor/TREVCAN-Explorer/venv/bin/python -c \
  'import can; print(can.__version__)'
```

Install the car service, again without `--enable-at-boot`:

```bash
bash telemetry/install_car_service.sh 192.168.0.136
```

When prompted, enter the exact `TELEMETRY_TOKEN` from the telemetry Pi. The
installer copies only the small sender package to `~/.local/share/trevcan`,
stores its private configuration in `~/.config/trevcan/car.env`, and uses the
canonical spool at `~/trevcan-data/car-spool.sqlite3`.

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
cd ~/TREVCAN-Explorer
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

## Running a vehicle test

After any telemetry-Pi reboot, a software lead performs this single preparation
step:

```bash
ssh pi@192.168.0.136
systemctl --user start trevcan-telemetry-control.service
```

Then the test operator does not need SSH:

1. Power the router, telemetry Pi, and car Pi.
2. Open `http://192.168.0.136:8080`.
3. Select **Start Test**. This starts the receiver first, waits for TCP port
   8765, and then starts the dual-bus car sender.
4. Open Grafana at `http://192.168.0.136:3000`. All provisioned dashboards
   include a **Telemetry tools** link back to TREVCAN Telemetry Tools.
5. Select **End Test - Stop Telemetry** when capture should stop. This cleanly
   stops and syncs only the telemetry sender. The car Pi and every other car
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

On the car Pi, the queue should normally remain small and return toward zero:

```bash
cd /home/trevor/.local/share/trevcan
/home/trevor/TREVCAN-Explorer/venv/bin/python -m telemetry.car \
  --status --db /home/trevor/trevcan-data/car-spool.sqlite3
```

On the telemetry Pi:

```bash
cd ~/TREVCAN-Explorer
.venv/bin/python -m telemetry.server \
  --status --db ~/trevcan-data/telemetry-server.sqlite3
sudo ss -ltnp | grep ':8765'
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Expected behavior:

- `last_acked` and the receiver stream checkpoint increase during capture.
- The car `queued` count fluctuates near zero. A temporary rise is acceptable
  if it subsequently drains.
- `pending_influx` remains bounded and falls after input stops.
- Grafana timestamps remain close to current time.
- Both `can0` and `can1` appear in the current data.

Service logs:

```bash
# Telemetry Pi
journalctl --user -u trevcan-telemetry-receiver.service -f
journalctl --user -u trevcan-telemetry-control.service -f

# Car Pi
journalctl --user -u trevcan-telemetry-car.service -f
```

## Data lifetime and recovery

- Car SQLite is a spool, not a 24-hour archive. ACKed rows are deleted;
  unacknowledged rows remain until delivered.
- Telemetry-Pi SQLite is a durable raw staging database. Successfully exported
  raw rows are retained for 15 minutes by default, then pruned. Unexported rows
  are never pruned by the age policy.
- The InfluxDB `home` bucket retains dashboard data for 24 hours.
- The same SQLite files are reused across Stop/Start and reconnects. Do not
  create a different database for every test.

An unexpected power cut can lose only frames still in the short uncommitted RAM
batch and can still damage an SD card. Already committed unacknowledged frames
remain in the car spool. Because boot startup is disabled, select **Start Test**
after the next boot to resume and drain them.

Do not delete an active SQLite file or its `-wal`, `-shm`, or `.lock` files.

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

### Car queue keeps growing

Check the receiver service, port 8765, receiver logs, `pending_influx`, LAN
connectivity, and both Pi clocks. A growing queue means the receiver is not
ACKing as fast as the car is capturing; changing Grafana refresh rate does not
fix transport backlog.

### DBC decode warnings appear

Raw frames are still stored and acknowledged. The current decoder routes these
DBCs by source bus:

- `can0`: BMS RTOS, HVC, VCU, inverter, and Baby MOBO DBCs.
- `can1`: DAQ, VCU, and `can9-database-01.09.dbc`.

Warnings about unexpected DLCs or multiplexor values indicate firmware/DBC
drift or an incomplete DBC definition. Fix the matching DBC or bus map; do not
reset the spool merely to hide decode warnings.

### Existing car applications consume high CPU

The telemetry sender passively reads SocketCAN and does not require the old
Explorer to be stopped. Do not disable unknown vehicle services without team
approval. If combined CPU load or temperature affects testing, coordinate with
the owner of `trev-dash.service` or `trevcan-explorer.service` before stopping
either one.

## Updating after code changes

Pull and reinstall the service package on each Pi. The installers remain
disabled-at-boot by default:

```bash
# Telemetry Pi
cd ~/TREVCAN-Explorer
git pull --ff-only
bash telemetry/install_receiver_service.sh
bash telemetry/install_control_service.sh --start-now

# Car Pi
cd ~/TREVCAN-Telemetry
git pull --ff-only
bash telemetry/install_car_service.sh 192.168.0.136
```

Only use `--enable-at-boot` later, after competition startup behavior and
power-loss recovery have been deliberately validated.
