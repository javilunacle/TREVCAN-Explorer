# TREVCAN Telemetry

Vehicle telemetry from the car Pi to a telemetry Pi, with Grafana Live for
current samples and InfluxDB for historical analysis.

```text
Car Pi: can0/can1 -> small RAM batches -> authenticated TCP
                                             |
Telemetry Pi: decode DBC signals -------------+
                 |                           |
                 v                           v
            Grafana Live              InfluxDB history
```

Disconnected data is discarded. Both Pis use bounded RAM buffers and resume
with fresh samples after reconnecting. Running distance and energy totals reset
when the receiver restarts. This is a passive monitoring pipeline.

Setup and test-day operation are documented in [telemetry/README.md](telemetry/README.md).

## Repository layout

- `telemetry/car.py`: multi-bus SocketCAN capture and sender.
- `telemetry/server.py`: receiver, DBC decoding, and historical export.
- `telemetry/live.py`: independent Grafana Live publisher.
- `telemetry/buffers.py`: bounded RAM queues.
- `telemetry/dbc_files/`: CAN message definitions.
- `telemetry/transforms.py` and `transform_policies.json`: timestamp-based calculations.
- `telemetry/control.py`: supervised test start/stop page.
- `telemetry/grafana/`: streaming and historical dashboards.
- `telemetry/scenario_runner.py` and `simulate.py`: synthetic validation tools.
- `docker-compose.telemetry-pi.yml`: Grafana and InfluxDB deployment.

## Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Configure `.env.telemetry` from [.env.telemetry.example](.env.telemetry.example),
then follow the runbook to provision Grafana and install the Pi services.
Installers leave boot startup disabled by default.

## Local validation

No CAN adapter, Grafana, or InfluxDB instance is needed for the tests:

```bash
python -m unittest discover -s telemetry -t .
python -m telemetry.grafana.render_provisioned
python -m telemetry.scenario_runner --list
```

See [the Grafana guide](telemetry/grafana/README.md) for channels, dashboards,
and the historical data model.

