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
Live query field selections retain the timestamp (`time` or `Time`) alongside
the plotted signals; omitting it causes Grafana's "Data is missing a time field" error.

The Live dashboard includes four key vehicle graphs, slip-ratio, launch-speed,
and inverter/rear-wheel RPM graphs, a static DBC-message
selector, selected-message samples/latest values, and recent raw frames. Its
one-minute view is limited to a browser buffer of at most 20,000 rows per query;
it starts when the viewer subscribes. Use capture timestamps to assess stale
values. Grafana Live is not the historical archive. The rest of the dashboards
use InfluxDB for historical analysis; their stable UIDs/URLs are preserved.

**Slip ratio (rear / front)** appears in Live Telemetry and Historical Analysis
(and the shared Synthetic Validation view). It calculates rear-wheel RPM divided
by average front-wheel RPM from the same VCU traction-control frame, independently
of `VCU_TC_Slip_Ratio`. `1.0` means equal wheel speeds; `1.1` means the rear wheels
are 10% faster. Zero front RPM or invalid front-wheel readings omit the sample.
Live uses `derived.Slip_Ratio` on the traction-control channel; history stores
`Slip_Ratio` in `telemetry_derived`, with the existing test-session selector.
Historical ratios are recorded after this update; earlier captures are not backfilled.
The Live slip-ratio panel displays **Waiting for valid wheel data** when its
buffer contains no usable ratio samples. A display-only row transformation
preserves valid ratios and removes null rows; missing ratios never become zero.

**Launch speed: target vs actual RPM** appears in Live Telemetry and Historical
VCU. It overlays `VCU_LS_Target_RPM` (blue) and `VCU_LS_Actual_RPM` (orange) from
`VCU_Launch_Speed` on the same RPM axis. Actual RPM retains its sign. The Live
panel subscribes to the message on both configured CAN buses; the Historical
VCU panel queries the recorded numeric signals for the selected time range.

**Inverter motor and rear wheel RPM** appears in Live Telemetry and Historical
Inverter. It overlays `INV_Motor_Speed` from the inverter on can0 (blue),
`DBL_WSPD_BL_RPM` from the rear-left DAQ on can1 (orange), and
`DBR_WSPD_BR_RPM` from the rear-right DAQ on can1 (green). All three use the same
RPM axis and preserve signed values. These are raw motor-shaft and wheel speeds,
without gearing correction or filtering on DAQ validity; check the rear-wheel
Valid and Timeout fields to assess whether a reported wheel speed is usable.

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
source update before their derived publication policy is applied. Transform
state stays in receiver RAM and advances after successful writes. Restarting
the receiver resets running totals; the session catalog contains metadata only.

The CAN-frame dashboard combines raw and decoded fields from the `can_frame`
measurement. Its recent-frames table is deliberately limited to the latest minute and
500 rows so Grafana does not pivot and sort the full dashboard time range on
every refresh. DBC-file and CAN-bus selectors make the combined table easier
to inspect; selecting **All** keeps unknown CAN IDs visible as
`Unknown / undecoded`. The subsystem dashboards query numeric decoded values
from `can_signal`, using its `dbc`, `message`, and `signal` tags to avoid mixing
unrelated data.

The receiver uses the first matching frame ID in its configured DBC order. The
dashboard signal names therefore match the order in
`telemetry/start_server_pi.sh`: BMS, HVC, inverter, master/VCU, then MOBO.
Overlapping IDs and the actual vehicle bus still require validation against the
team-approved DBC set.

## Scope and safety boundary

Grafana provides monitoring for BMS, HVC, inverter, VCU, and MOBO telemetry.
The pipeline does not transmit CAN commands or flash firmware. Telemetry Tools
controls only capture start/stop; it cannot power off the vehicle.

The dashboards are only as accurate as the active DBCs, clocks, and source
frames. Synthetic replay proves the query/display path but does not validate
the car wiring, real message cadence, sustained throughput, signal semantics,
or power-loss behavior. Validate every panel with real frames before treating
using it for test-day monitoring.
