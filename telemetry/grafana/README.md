# TREVCAN Grafana dashboards

The telemetry-Pi deployment provisions seven **read-only** dashboards from
this directory:

- `TREVCAN - Vehicle Overview`
- `TREVCAN - BMS`
- `TREVCAN - HVC`
- `TREVCAN - Inverter`
- `TREVCAN - VCU`
- `TREVCAN - MOBO`
- `TREVCAN CAN Explorer (combined draft)`

The subsystem dashboards replace the original catch-all Systems graph. Signals
with different units are split into separate panels, legends display the signal
name, and high-cardinality BMS data uses a module selector plus last-value
tables/bar gauges instead of plotting the entire accumulator in one graph.

`render_provisioned.py` combines the importable CAN Explorer draft with the
dashboard definitions in `dashboard_specs.py` and writes the results to the
ignored `generated/` directory. `telemetry/start_server_pi.sh` runs the renderer
before starting Docker. Grafana loads the rendered files through its file
provisioner and stores user preferences in its persistent Docker volume.

The dashboards refresh every five seconds by default. A one-second option is
available when a faster live view is useful. A shorter interval increases load
on both Grafana and InfluxDB, particularly when several viewers are connected.

## Data model

The CAN Explorer combines raw and decoded fields from the `can_frame`
measurement. Unknown CAN IDs still appear with blank decoded columns. The
subsystem dashboards query numeric decoded values from `can_signal`, using its
`dbc`, `message`, and `signal` tags to avoid mixing unrelated data.

The receiver uses the first matching frame ID in its configured DBC order. The
dashboard signal names therefore match the order in
`telemetry/start_server_pi.sh`: BMS, HVC, inverter, master/VCU, then MOBO.
Overlapping IDs and the actual vehicle bus still require validation against the
team-approved DBC set.

## Scope and safety boundary

Grafana replaces the monitoring portions of the React BMS, HVC, inverter, VCU,
and MOBO pages. It does **not** reproduce Balance Manager, Module Config, reset,
relay, passthrough, inverter-command, or other CAN-transmit controls. Those
controls need a separately authenticated and reviewed command path; they must
not be exposed as general Grafana actions.

The dashboards are only as accurate as the active DBCs, clocks, and source
frames. Synthetic replay proves the query/display path but does not validate
the car wiring, real message cadence, sustained throughput, signal semantics,
or power-loss behavior. Validate every panel with real frames before treating
it as the operational replacement for the React Explorer.

## Optional backfill for older raw data

Frames exported before the combined Explorer fields were added retain their raw
data but do not automatically gain the decoded summary. A demo database can be
backfilled without modifying the raw SQLite archive or car spool. First dry-run:

```powershell
.\.venv\Scripts\python.exe -m telemetry.backfill --db telemetry-server.sqlite3 --dbc webserver/backend/dbc_files/master.dbc
```

If the count is expected, add `--write`. The command reads `INFLUXDB_TOKEN` from
the environment or prompts for it. Never place the token directly in a command
or commit it. This backfill is for historical visualization only and does not
create new `can_signal` points.
