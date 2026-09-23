# Provisional TREVCAN Grafana dashboards

These are importable, **read-only drafts** for the InfluxDB v2 `home` bucket used by
the telemetry prototype. They do not replace the React Explorer yet, and they do
not send CAN commands.

## Import into the existing demo Grafana

1. Open Grafana at `http://localhost:3000` on the laptop.
2. Choose **Dashboards → New → Import** and upload
   `trevcan-can-explorer.draft.json`.
3. When prompted, select the existing Flux-enabled `influxdb` datasource.
4. Repeat for `trevcan-systems.draft.json`.

The combined Explorer draft has a distinct dashboard UID, so importing it
creates a new dashboard rather than overwriting the earlier Explorer draft
already in Grafana.

Importing these files does not change the running receiver or InfluxDB. The
queries assume bucket `home` and Grafana's dashboard time range. The draft
dashboards refresh every five seconds by default. The telemetry-Pi deployment
also allows selecting a one-second refresh interval when a faster live view is
useful.

The CAN Explorer draft has a combined raw-and-decoded frame table (`can_frame`)
and graphs driven by decoded signals (`can_signal`). New exports put the DBC
message name and a readable signal summary in the **same row** as the raw ID,
hex bytes, and sequence number. Unknown CAN IDs still appear with blank decoded
columns. Frames exported before this change keep their raw fields but do not
automatically gain a decoded summary. To add summaries to the existing demo
history without changing the raw SQLite archive or car spool, first dry-run:

```powershell
.\.venv\Scripts\python.exe -m telemetry.backfill --db telemetry-server.sqlite3 --dbc webserver/backend/dbc_files/master.dbc
```

If the count is expected, add `--write` to the same command. It uses
`INFLUXDB_TOKEN` from the current PowerShell session or prompts for a token;
never put the token directly on the command line. The default write URL targets
the demo's `docs` organization and `home` bucket. Re-running `--write` is safe
for this demo because the same measurement, tags, and nanosecond timestamp
identify each `can_frame` point in InfluxDB. Only frames already marked
`influx_done=1` are considered. This does not create new `can_signal` points.

For new live data, restart the receiver so it loads the updated exporter. The
original `--simulate-rate` mode generates only ID 160, so most Systems panels
will show **No data** in that mode. To populate the draft Systems panels with
synthetic frames from the dashboard DBCs, use the `telemetry.simulate`
generator and `--simulate-file` replay described in `telemetry/README.md`.

## Scope and gaps

- The Systems draft is an initial signal browser for BMS, HVC, VCU, MOBO, and
  inverter data. It is **not** visual or functional parity with the React
  dashboards. In particular, BMS cell layout, state/enum formatting, stale-data
  handling, warnings, and per-component derived values need dedicated panels.
- The Balance Manager, Module Config, and other CAN-transmit controls are omitted.
  Their behavior must not be recreated as broadly accessible Grafana controls
  without a separate reviewed command path.
- The `dual_bus` branch (commit `cd64e3d`) has additional DBCs and Driving,
  DAQ, and VCU launch-control views. Those are not in these initial drafts.
- The current receiver decodes with the first matching DBC in `--dbc` order.
  Resolve overlapping IDs and bus identity before loading every DBC or claiming
  that all car signals are decoded correctly.
- These files are stored in the repo only. Grafana will not display them until
  they are imported. The current Compose service has no Grafana data volume,
  so recreate/persistence work is still required before relying on saved
  dashboards on the telemetry Pi.

Use the actual car's CAN frames and the team's approved DBC set to validate
each panel before treating this as a replacement for the current Explorer.
