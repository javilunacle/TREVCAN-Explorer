# Telemetry DBC library

The telemetry receiver, dashboard renderer, and simulation tools load CAN
definitions from this directory. The original DBC contents are preserved.

Default receiver priority in `telemetry/start_server_pi.sh`:

| Bus | DBC files, in priority order |
| --- | --- |
| `can0` | BMS-Firmware-RTOS-Complete, hvc, VCU, BMS-Inverter-Only, Baby_MOBO |
| `can1` | DAQ-Firmware, VCU, can9-database-01.09 |

Bus routing matters when identifiers overlap. Keep the Live dashboard catalog
in `telemetry/grafana/live_dashboard.py` aligned with the receiver's routing.
Other definitions remain available for explicit `--dbc` / `--dbc-bus` use and
decoder fixtures. No upload history, GUI configuration, or transmit lists are
stored here.
