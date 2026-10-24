# TREVCAN Grafana dashboards

The telemetry-Pi deployment provisions ten monitoring dashboards from this
directory:

- `TREVCAN - Live Telemetry`
- `TREVCAN - Historical Vehicle Overview`
- `TREVCAN - Historical Analysis`
- `TREVCAN - Historical Synthetic Validation`
- `TREVCAN - Historical BMS`
- `TREVCAN - Historical HVC`
- `TREVCAN - Historical Inverter`
- `TREVCAN - Historical VCU`
- `TREVCAN - Historical MOBO`
- `TREVCAN - Historical CAN Frames`

**Live Telemetry** uses the built-in `-- Grafana --` data source's Live
Measurements queries, with no InfluxDB queries or polling selectors. The
receiver pushes small batches to `/api/live/push/trevcan`. A message has a
bus/ID/extended-aware channel, such as `stream/trevcan/can0_000000a0_std`.
Wide fields `signal.NAME` carry every numeric decoded update; `state.NAME`
preserves textual/enum labels. `stream/trevcan/can_frames` carries every fresh
raw frame with its bus, ID, sequence, bytes, DBC, and decoded summary. Capture
timestamps are preserved. Synthetic scenarios use `trevcan_synthetic` instead.

The Live dashboard includes four key vehicle graphs, a static DBC-message
selector, selected-message samples/latest values, recent raw frames, and a
calculated-signal selector/graph. Calculated signals use channels such as
`stream/trevcan/derived_Vehicle_Speed_mps`. The browser keeps a bounded recent
window starting when the viewer subscribes; it does not load the entire run. Use capture timestamps to assess stale
values. Grafana Live is not the historical archive. The rest of the dashboards
use InfluxDB for historical analysis; their stable UIDs/URLs are preserved.

The subsystem dashboards replace the original catch-all Systems graph. Signals
with different units are split into separate panels, legends display the signal
name, and high-cardinality BMS data uses a module selector plus last-value
tables/bar gauges instead of plotting the entire accumulator in one graph.

`render_provisioned.py` combines the historical CAN-frame template with the
dashboard definitions in `dashboard_specs.py` and writes the results to the
ignored `generated/` directory. `telemetry/start_server_pi.sh` runs the renderer
before starting Docker. Grafana loads the rendered files through its file
provisioner and stores user preferences in its persistent Docker volume.

Historical dashboards refresh every five seconds by default. A one-second option is
available. A shorter interval increases load
on both Grafana and InfluxDB, particularly when several viewers are connected.

## Historical data model

During healthy capture, InfluxDB receives every raw frame and every numeric
decoded sample, rather than the latest value from each export batch. Outages
leave gaps: neither Pi keeps a SQLite replay queue. Historical chart queries may
downsample for display; integrations and derivatives consume every selected
source update before their publication policy is applied. Fast outputs publish
on each eligible input; medium/slow outputs retain their configured spacing.
`process_frame()` advances RAM state once before the two output workers run,
independently of InfluxDB writes. Restarting the receiver resets running totals;
the session catalog contains metadata only.

The CAN-frame dashboard combines raw and decoded fields from the `can_frame`
measurement. Its recorded-frames table returns each stored frame in the
selected Grafana time range, without `last()` or a fixed 500-row limit. CSV
exports contain the query results for that selection. Narrow the range and
filters when reviewing high-rate runs to avoid loading millions of browser
rows at once. DBC-file and CAN-bus selectors make the combined table easier
to inspect; selecting **All** keeps unknown CAN IDs visible as
`Unknown / undecoded`. The subsystem dashboards query numeric decoded values
from `can_signal`, using its `dbc`, `message`, and `signal` tags to avoid mixing
unrelated data.

CANManager frames carry their DBC/message identity and are never re-decoded.
For raw-only diagnostic inputs, the receiver uses its configured DBC order
from `telemetry/start_server_pi.sh`. Car frames instead retain the active
CANManager definition's filename and signal names. Inverter history queries
accept both `Inverter.dbc` (the car) and `BMS-Inverter-Only.dbc` (earlier or
diagnostic decoding); the Live inverter selector labels use `Inverter.dbc`.

The car catalog now includes BMS-complete on `can0`, GPS/IMU on `can1`, and
a lower-priority DAQ fallback for missing message IDs. No car dashboard gauge
is required for these messages to reach Grafana and InfluxDB. Other reference
DBCs are not interchangeable: HVC and MOBO panels/selectors still need their
reference signal names aligned with the car's active definitions. HVC pack
totals also await the confirmed Hi/Lo combination rule. Overlapping IDs and
the actual vehicle bus still require validation against the team-approved
DBC set.

## Scope and safety boundary

Grafana provides monitoring for BMS, HVC, inverter, VCU, and MOBO telemetry.
The pipeline does not transmit CAN commands or flash firmware. Telemetry Tools
controls only capture start/stop; it cannot power off the vehicle.

The dashboards are only as accurate as the active DBCs, clocks, and source
frames. Synthetic replay proves the query/display path but does not validate
the car wiring, real message cadence, sustained throughput, signal semantics,
or power-loss behavior. Validate every panel with real frames before treating
using it for test-day monitoring.
