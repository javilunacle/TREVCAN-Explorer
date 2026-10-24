"""Grafana Live views using static DBC selectors, without Influx queries."""

import os
from pathlib import Path

import cantools

from telemetry.live import live_measurement, stream_namespace
from telemetry.grafana.dashboard_specs import (
    TEMPERATURE_DESCRIPTION, TEMPERATURE_SOURCES, _dashboard,
)


LIVE_DATASOURCE = {"type": "grafana", "uid": "grafana"}
DBC_DIRECTORY = Path(__file__).resolve().parents[1] / "dbc_files"
BUS_DBCS = {
    "can0": ["BMS-Firmware-RTOS-Complete.dbc", "hvc.dbc", "VCU.dbc",
             "BMS-Inverter-Only.dbc", "Baby_MOBO.dbc"],
    "can1": ["DAQ-Firmware.dbc", "VCU.dbc", "can9-database-01.09.dbc"],
}


def channel_catalog():
    """Match receiver DBC priority; identical CAN IDs on different buses stay separate."""
    catalog = {}
    databases = {}
    for bus, filenames in BUS_DBCS.items():
        for filename in filenames:
            if filename not in databases:
                databases[filename] = cantools.database.load_file(
                    DBC_DIRECTORY / filename, strict=False)
            for message in databases[filename].messages:
                channel = live_measurement(bus, message.frame_id, message.is_extended_frame)
                catalog.setdefault(channel, {
                    "bus": bus, "dbc": filename, "message": message.name,
                    "signals": {signal.name for signal in message.signals},
                    "channel": channel,
                })
    return sorted(catalog.values(), key=lambda item: (item["bus"], item["message"]))


def _target(namespace, channel, *, fields=None):
    target = {"refId": "A", "datasource": LIVE_DATASOURCE,
              "queryType": "measurements", "channel": f"stream/{namespace}/{channel}",
              "buffer": 60000}
    if fields:
        # Grafana Live filters every field by name, including the timestamp.
        # Retain both timestamp spellings supported by our Live views.
        target["filter"] = {"fields": list(dict.fromkeys(["time", "Time", *fields]))}
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
        panels.append(_panel(index + 1, title,
                             {"x": (index % 2) * 12, "y": (index // 2) * 7, "w": 12, "h": 7},
                             _target(namespace, message["channel"], fields=[f"signal.{signal}"]),
                             unit=unit))
    traction = [item for item in catalog if item["dbc"] == "VCU.dbc" and
                item["message"] == "VCU_Traction_Control"]
    slip = _panel(8, "Slip ratio (rear / front)",
                  {"x": 0, "y": 14, "w": 12, "h": 7},
                  _target(namespace, traction[0]["channel"], fields=["derived.Slip_Ratio"]),
                  unit="suffix:x")
    slip["targets"] = []
    for index, message in enumerate(traction):
        target = _target(namespace, message["channel"], fields=["derived.Slip_Ratio"])
        target["refId"] = chr(ord("A") + index)
        slip["targets"].append(target)
        slip["fieldConfig"]["overrides"].append({
            "matcher": {"id": "byFrameRefID", "options": target["refId"]},
            "properties": [{"id": "displayName", "value": f"Slip ratio ({message['bus']})"}],
        })
    slip["description"] += (
        " Rear-wheel RPM / average front-wheel RPM from the same VCU traction-control "
        "sample. 1.0 = no slip; 1.1 = rear wheels 10% faster. Requires both front "
        "validity flags and positive front RPM; invalid samples are omitted.")
    slip["fieldConfig"]["defaults"].update({
        "displayName": "Slip ratio", "decimals": 3,
        "noValue": "Waiting for valid wheel data",
    })
    # A time-only stream is a normal waiting state until the first valid ratio.
    # Reduce each row separately: missing ratio -> null, never a synthetic zero.
    # Keep each source frame separate, retain time, then omit the null rows so
    # Grafana uses noValue instead of its missing-number-field schema warning.
    slip["transformations"] = [
        {"id": "calculateField", "options": {
            "mode": "reduceRow", "timeSeries": False,
            "reduce": {"include": ["derived.Slip_Ratio"], "reducer": "lastNotNull"},
            "alias": "Slip ratio", "replaceFields": False,
        }},
        {"id": "filterFieldsByName", "options": {
            "include": {"names": ["time", "Time", "Slip ratio"]},
        }},
        {"id": "filterByValue", "options": {
            "type": "include", "match": "all",
            "filters": [{"fieldName": "Slip ratio", "config": {"id": "isNotNull"}}],
        }},
        {"id": "filterFields", "options": {
            "exclude": {"id": "byValue", "options": {"reducer": "allIsNull"}},
        }},
    ]
    panels.append(slip)
    launch_fields = ["signal.VCU_LS_Target_RPM", "signal.VCU_LS_Actual_RPM"]
    launch_messages = [item for item in catalog if item["dbc"] == "VCU.dbc" and
                       item["message"] == "VCU_Launch_Speed"]
    launch = _panel(9, "Launch speed: target vs actual RPM",
                    {"x": 12, "y": 14, "w": 12, "h": 7},
                    _target(namespace, launch_messages[0]["channel"], fields=launch_fields),
                    unit="suffix:RPM")
    launch["targets"] = [
        {**_target(namespace, message["channel"], fields=launch_fields),
         "refId": chr(ord("A") + index)}
        for index, message in enumerate(launch_messages)
    ]
    launch["description"] += (
        " Target and actual RPM from the same VCU_Launch_Speed frame, "
        "overlaid on one RPM axis. Actual RPM preserves its signed value.")
    launch["fieldConfig"]["defaults"]["decimals"] = 0
    for signal, label, color in (("Target", "Target RPM", "#5794F2"),
                                 ("Actual", "Actual RPM", "#FF9830")):
        launch["fieldConfig"]["overrides"].append({
            "matcher": {"id": "byRegexp", "options": f"^signal\\.VCU_LS_{signal}_RPM(?: .*)?$"},
            "properties": [{"id": "displayName", "value": label},
                           {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        })
    panels.append(launch)
    speed_sources = [
        ("can0", "BMS-Inverter-Only.dbc", "INV_Motor_Speed", "Inverter motor RPM", "#5794F2"),
        ("can1", "DAQ-Firmware.dbc", "DBL_WSPD_BL_RPM", "Rear-left wheel RPM", "#FF9830"),
        ("can1", "DAQ-Firmware.dbc", "DBR_WSPD_BR_RPM", "Rear-right wheel RPM", "#73BF69"),
    ]
    speed_targets, speed_overrides = [], []
    for index, (bus, dbc, signal, label, color) in enumerate(speed_sources):
        message = next(item for item in catalog if item["bus"] == bus and
                       item["dbc"] == dbc and signal in item["signals"])
        speed_targets.append({
            **_target(namespace, message["channel"], fields=[f"signal.{signal}"]),
            "refId": chr(ord("A") + index),
        })
        speed_overrides.append({
            "matcher": {"id": "byRegexp", "options": f"^signal\\.{signal}(?: .*)?$"},
            "properties": [{"id": "displayName", "value": label},
                           {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        })
    speeds = _panel(10, "Inverter motor and rear wheel RPM",
                    {"x": 0, "y": 21, "w": 24, "h": 7}, speed_targets[0],
                    unit="suffix:RPM")
    speeds["targets"] = speed_targets
    speeds["fieldConfig"]["overrides"] = speed_overrides
    speeds["fieldConfig"]["defaults"]["decimals"] = 0
    speeds["description"] += (
        " Raw inverter motor-shaft RPM and rear-left/rear-right wheel RPM on one "
        "axis, with no gearing correction. Signed values are preserved. Check "
        "the DAQ rear-wheel Valid and Timeout signals when assessing wheel readings.")
    panels.append(speeds)
    temperature_targets = []
    for index, (bus, dbc, name, signals) in enumerate(TEMPERATURE_SOURCES):
        message = next(item for item in catalog if item["bus"] == bus and
                       item["dbc"] == dbc and item["message"] == name)
        temperature_targets.append({
            **_target(namespace, message["channel"],
                      fields=[f"signal.{signal}" for signal, _ in signals]),
            "refId": chr(ord("A") + index),
        })
    temperatures = _panel(11, "Vehicle temperatures",
                          {"x": 0, "y": 28, "w": 24, "h": 12},
                          temperature_targets[0], unit="celsius")
    temperatures["targets"] = temperature_targets
    temperatures["description"] += " " + TEMPERATURE_DESCRIPTION
    temperatures["fieldConfig"]["defaults"]["decimals"] = 1
    temperatures["options"]["legend"]["placement"] = "right"
    temperatures["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byRegexp", "options": f"^signal\\.{signal}(?: .*)?$"},
         "properties": [{"id": "displayName", "value": label}]}
        for _, _, _, signals in TEMPERATURE_SOURCES for signal, label in signals
    ]
    panels.append(temperatures)
    selected = _panel(5, "Selected message — decoded samples",
                      {"x": 0, "y": 40, "w": 16, "h": 9},
                      _target(namespace, "${can_message}"))
    selected["transformations"] = [{"id": "filterFieldsByName", "options": {
        "include": {"pattern": "^(Time|time|signal\\..*)$"}}}]
    panels.append(selected)
    values = _panel(6, "Selected message — latest values",
                    {"x": 16, "y": 40, "w": 8, "h": 9},
                    _target(namespace, "${can_message}"), kind="table")
    values["transformations"] = [{"id": "reduce", "options": {
        "reducers": ["lastNotNull"], "mode": "seriesToRows"}}]
    panels.append(values)
    panels.append(_panel(7, "Live CAN frames — recent arrivals",
                         {"x": 0, "y": 49, "w": 24, "h": 10},
                         _target(namespace, "can_frames"), kind="table"))
    dashboard = _dashboard("trevcan-live-telemetry", "TREVCAN - Live Telemetry",
                           "Streaming decoded CAN samples and raw frames. No InfluxDB polling. "
                           "Synthetic scenarios use separate channels.", panels,
                           variables=[variable])
    dashboard["refresh"] = ""
    dashboard["liveNow"] = True
    dashboard["time"] = {"from": "now-1m", "to": "now"}
    dashboard["tags"].append("live")
    return dashboard
