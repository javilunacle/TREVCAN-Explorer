# TREVCAN Telemetry project guidelines

This repository contains a passive two-Pi telemetry pipeline. The car Pi reads
SocketCAN through dashboard CANManager and sends raw/decoded TCP batches. The
telemetry Pi calculates once per accepted frame, then publishes the same samples
to Grafana Live and InfluxDB. Raw-only diagnostic inputs decode once on receipt.

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
- Keep Grafana Live and calculation state independent of InfluxDB writes.
- Fast calculated outputs follow eligible inputs (`publish_period_ms: 0`).
- Use original capture timestamps for historical samples and calculations.
- Keep synthetic `scenario-*` streams out of real-vehicle dashboard channels.
- Keep CAN capture passive; simulation never transmits onto SocketCAN.
- Keep service boot startup disabled unless explicitly requested.
- Keep secrets out of source files, dashboard JSON, and logs.

## Validation

```bash
python -m telemetry.grafana.render_provisioned
python -m telemetry.scenario_runner --list
```

These checks run without CAN hardware or database services. Deployment and measured
Pi throughput/latency require separate hardware validation. Read
`telemetry/README.md` for installation and test-day operations.
