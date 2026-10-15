# TREVCAN Telemetry project guidelines

This repository contains a passive two-Pi telemetry pipeline. The car Pi reads
SocketCAN and sends small TCP batches. The telemetry Pi decodes DBC signals,
publishes current samples to Grafana Live, and records history in InfluxDB.

## Architecture

- `telemetry/car.py`: SocketCAN capture, simulation/replay input, TCP sender.
- `telemetry/buffers.py`: bounded RAM batching with age/capacity limits.
- `telemetry/server.py`: authenticated receiver, DBC routing, historical export.
- `telemetry/live.py`: independent Grafana Live HTTP publisher.
- `telemetry/dbc_files/`: message definitions shared by decoder and simulations.
- `telemetry/transforms.py`: source-timestamp integrals, derivatives, and diagnostics.
- `telemetry/grafana/`: provisioned Live and historical dashboards.
- `telemetry/control.py`: supervised test start/stop controls.
- `telemetry/systemd/`: Pi service templates.

## Runtime policy

- Discard disconnected data and resume fresh; do not introduce disk spooling.
- Keep memory bounded. ACKs acknowledge RAM acceptance, not durable recording.
- Preserve every decoded update within a healthy publication batch.
- Keep Grafana Live independent of InfluxDB write latency/failure.
- Use original capture timestamps for historical samples and calculations.
- Keep synthetic `scenario-*` streams out of real-vehicle dashboard channels.
- Keep CAN capture passive; simulation never transmits onto SocketCAN.
- Keep service boot startup disabled unless explicitly requested.
- Keep secrets out of source files, dashboard JSON, and logs.

## Validation

```bash
python -m unittest discover -s telemetry -t .
python -m telemetry.grafana.render_provisioned
python -m telemetry.scenario_runner --list
```

Tests run without CAN hardware or database services. Deployment and measured
Pi throughput/latency require separate hardware validation. Read
`telemetry/README.md` for installation and test-day operations.
