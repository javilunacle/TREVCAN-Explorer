"""Grafana Live views using static DBC selectors, without Influx queries."""

import os
from pathlib import Path

import cantools

from telemetry.live import live_measurement, stream_namespace
from telemetry.grafana.dashboard_specs import _custom_variable, _dashboard


LIVE_DATASOURCE = {"type": "grafana", "uid": "grafana"}
DBC_DIRECTORY = Path(__file__).resolve().parents[1] / "dbc_files"
BUS_DBCS = {
    # On the car, Inverter.dbc wins the shared inverter-command ID on can0.
    "can0": ["BMS-Firmware-RTOS-Complete.dbc", "hvc.dbc", "BMS-Inverter-Only.dbc",
             "VCU.dbc", "Baby_MOBO.dbc"],
    "can1": ["DAQ-Firmware.dbc", "VCU.dbc", "can9-database-01.09.dbc"],
}


# These names are the engine's public outputs. Each has a stable Live channel;
# selecting one changes the graph subscription without querying InfluxDB.
CALCULATED_SIGNALS = (
    "Vehicle_Speed_mps", "Vehicle_Accel_From_Speed_mps2", "Distance_m",
    "Pack_Power_W", "Charge_Added_Ah", "Charge_Removed_Ah",
    "Energy_Charged_Wh", "Energy_Used_Wh", "Net_Energy_Change_Wh",
    "Accumulator_Temp_Rise_C_per_s", "IMU_Alignment_Valid",
    "Longitudinal_Accel_g", "Lateral_Accel_g", "Combined_Accel_g",
)


def channel_catalog():
    """Build reference selectors; actual values come from CANManager frames."""
    catalog = {}
    databases = {}
    for bus, filenames in BUS_DBCS.items():
        for filename in filenames:
            if filename not in databases:
                databases[filename] = cantools.database.load_file(
                    DBC_DIRECTORY / filename, strict=False)
            for message in databases[filename].messages:
                channel = live_measurement(bus, message.frame_id, message.is_extended_frame)
                # These inverter definitions match CANManager exactly; use
                # its source filename in selector labels. Channels stay bus/ID based.
                source = "Inverter.dbc" if filename == "BMS-Inverter-Only.dbc" else filename
                catalog.setdefault(channel, {
                    "bus": bus, "dbc": source, "message": message.name,
                    "signals": {signal.name for signal in message.signals},
                    "channel": channel,
                })
    return sorted(catalog.values(), key=lambda item: (item["bus"], item["message"]))


def _target(namespace, channel, *, fields=None):
    target = {"refId": "A", "datasource": LIVE_DATASOURCE,
              "queryType": "measurements", "channel": f"stream/{namespace}/{channel}",
              "buffer": 60000}
    if fields:
        target["filter"] = {"fields": fields}
    return target


def _panel(panel_id, title, grid, target, *, kind="timeseries", unit=None):
    defaults = {"custom": {"spanNulls": False}, "noValue": "No live samples"}
    if unit:
        defaults["unit"] = unit
    panel = {"id": panel_id, "title": title, "type": kind, "gridPos": grid,
             "datasource": LIVE_DATASOURCE, "targets": [target],
             "maxDataPoints": 10000, "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"legend": {"displayMode": "table", "placement": "bottom",
                                      "calcs": ["lastNotNull"]},
                         "tooltip": {"mode": "multi"}},
             "description": "Grafana Live subscription. Values use the car's capture timestamps. "
                            "The browser retains a bounded recent window; use Historical dashboards "
                            "for earlier samples. Check timestamps when capture stops."}
    if kind == "table":
        panel["options"] = {"showHeader": True, "sortBy": [{"displayName": "Time", "desc": True}]}
    return panel


def live_dashboard():
    namespace = stream_namespace(os.getenv("GRAFANA_LIVE_URL") or
                                 "http://127.0.0.1:3000/api/live/push/trevcan")
    catalog = channel_catalog()
    options = [{"text": f"{item['bus']} / {item['dbc']} / {item['message']}",
                "value": item["channel"], "selected": False} for item in catalog]
    current = options[0]
    # Prefer the actual speed message instead of assuming a firmware CAN ID.
    speed = next((item for item in catalog if item["bus"] == "can0" and
                  "VCU_Speed_MPH" in item["signals"]), None)
    if speed:
        current = next(item for item in options if item["value"] == speed["channel"])
    current["selected"] = True
    variable = {"name": "can_message", "label": "CAN message", "type": "custom",
                "query": ",".join(f"{item['text']} : {item['value']}" for item in options),
                "options": options, "current": current.copy(), "multi": False,
                "includeAll": False, "hide": 0, "skipUrlSync": False}
    panels = []
    overview = [
        ("Vehicle speed", "VCU_Speed_MPH", "VCU.dbc", "velocitymph"),
        ("Pack voltage", "HVC_Batt_Voltage_V", "hvc.dbc", "volt"),
        ("Pack current", "HVC_Pack_Current_A", "hvc.dbc", "amp"),
        ("Accumulator temperature", "HVC_Acc_Temp_Max_C", "hvc.dbc", "celsius"),
    ]
    for index, (title, signal, dbc, unit) in enumerate(overview):
        message = next(item for item in catalog if item["bus"] == "can0" and
                       item["dbc"] == dbc and signal in item["signals"])
        fields = [f"signal.{signal}"]
        panels.append(_panel(index + 1, title,
                             {"x": (index % 2) * 12, "y": (index // 2) * 7, "w": 12, "h": 7},
                             _target(namespace, message["channel"], fields=fields),
                             unit=unit))
    selected = _panel(5, "Selected message â€” decoded samples",
                      {"x": 0, "y": 14, "w": 16, "h": 9},
                      _target(namespace, "${can_message}"))
    selected["transformations"] = [{"id": "filterFieldsByName", "options": {
        "include": {"pattern": "^(Time|time|signal\\..*)$"}}}]
    panels.append(selected)
    values = _panel(6, "Selected message â€” latest values",
                    {"x": 16, "y": 14, "w": 8, "h": 9},
                    _target(namespace, "${can_message}"), kind="table")
    values["transformations"] = [{"id": "reduce", "options": {
        "reducers": ["lastNotNull"], "mode": "seriesToRows"}}]
    panels.append(values)
    panels.append(_panel(7, "Live CAN frames â€” recent arrivals",
                         {"x": 0, "y": 23, "w": 24, "h": 10},
                         _target(namespace, "can_frames"), kind="table"))
    calculated = _panel(8, "Selected calculated signal",
                        {"x": 0, "y": 33, "w": 24, "h": 9},
                        _target(namespace, "derived_${calculated_signal}", fields=["value"]))
    calculated["description"] = (
        "Calculated values use source capture timestamps. Fast signals update "
        "on each eligible input; other signals follow their publication policy.")
    panels.append(calculated)
    calculated_variable = _custom_variable(
        "calculated_signal", "Calculated signal", CALCULATED_SIGNALS, "Vehicle_Speed_mps")
    dashboard = _dashboard("trevcan-live-telemetry", "TREVCAN - Live Telemetry",
                           "Streaming decoded CAN samples and raw frames. No InfluxDB polling. "
                           "Synthetic scenarios use separate channels.", panels,
                           variables=[variable, calculated_variable])
    dashboard["refresh"] = ""
    dashboard["liveNow"] = True
    dashboard["time"] = {"from": "now-1m", "to": "now"}
    dashboard["tags"].append("live")
    return dashboard
