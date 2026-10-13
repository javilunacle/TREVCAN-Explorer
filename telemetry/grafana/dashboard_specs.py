"""Readable, read-only Grafana dashboards for the off-car telemetry stack.

The React application combines monitoring with commands and configuration.
These dashboards intentionally reproduce only its monitoring views. They use
the bus-specific DBC routing configured by ``telemetry/start_server_pi.sh``.
"""

DATASOURCE = {"type": "influxdb", "uid": "trevcan-influxdb"}
REFRESH_INTERVALS = ["1s", "2s", "5s"]
QUICK_RANGES = [
    {"display": "Last 5 minutes", "from": "now-5m", "to": "now"},
    {"display": "Last 15 minutes", "from": "now-15m", "to": "now"},
    {"display": "Last 30 minutes", "from": "now-30m", "to": "now"},
    {"display": "Last 1 hour", "from": "now-1h", "to": "now"},
    {"display": "Last 6 hours", "from": "now-6h", "to": "now"},
    {"display": "Last 12 hours", "from": "now-12h", "to": "now"},
    {"display": "Last 24 hours", "from": "now-24h", "to": "now"},
]


def _signal_query(condition, *, dbc=None, latest=False, table=False, aggregate=False):
    filters = [
        'r._measurement == "can_signal"',
        'r._field == "value"',
        f"({condition})",
    ]
    if dbc:
        filters.append(f'r.dbc == "{dbc}"')
    query = (
        'from(bucket: "home")\n'
        '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        f"  |> filter(fn: (r) => {' and '.join(filters)})\n"
        '  |> group(columns: ["signal"])'
    )
    if latest:
        query += '\n  |> last()'
    elif aggregate:
        query += '\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)'
    if table:
        query += (
            '\n  |> keep(columns: ["_time", "message", "signal", "_value"])'
            '\n  |> group()'
            '\n  |> rename(columns: {_time: "time", _value: "value"})'
            '\n  |> sort(columns: ["signal"])'
        )
    else:
        query += '\n  |> keep(columns: ["_time", "_value", "signal"])'
    return query


def _derived_query(condition, *, latest=False, aggregate=False):
    query = (
        'from(bucket: "home")\n'
        '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        '  |> filter(fn: (r) => r._measurement == "telemetry_derived" and '
        f'r._field == "value" and ({condition}))\n'
        '  |> group(columns: ["signal"])'
    )
    if latest:
        query += '\n  |> last()'
    elif aggregate:
        query += '\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)'
    return query + '\n  |> keep(columns: ["_time", "_value", "signal", "unit", "session"])'


def _target(query, result_format="time_series"):
    return {
        "refId": "A",
        "datasource": DATASOURCE,
        "query": query,
        "rawQuery": True,
        "resultFormat": result_format,
    }


def _field_defaults(unit=None, *, minimum=None, maximum=None, decimals=None):
    defaults = {"displayName": "${__field.labels.signal}"}
    if unit:
        defaults["unit"] = unit
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    if decimals is not None:
        defaults["decimals"] = decimals
    return defaults


def timeseries(panel_id, title, condition, grid, *, dbc=None, unit=None,
               description="", minimum=None, maximum=None, decimals=None):
    return {
        "id": panel_id,
        "title": title,
        "type": "timeseries",
        "description": description,
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(_signal_query(condition, dbc=dbc, aggregate=True))],
        "fieldConfig": {
            "defaults": _field_defaults(
                unit, minimum=minimum, maximum=maximum, decimals=decimals
            ),
            "overrides": [],
        },
        "options": {
            "legend": {
                "calcs": ["lastNotNull"],
                "displayMode": "table",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    }


def stat(panel_id, title, signal, grid, *, dbc=None, unit=None,
         description="", minimum=None, maximum=None, decimals=None,
         extra_condition=None):
    condition = f'r.signal == "{signal}"'
    if extra_condition:
        condition = f"({condition}) and ({extra_condition})"
    return {
        "id": panel_id,
        "title": title,
        "type": "stat",
        "description": description,
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(_signal_query(condition, dbc=dbc, latest=True))],
        "fieldConfig": {
            "defaults": _field_defaults(
                unit, minimum=minimum, maximum=maximum, decimals=decimals
            ),
            "overrides": [],
        },
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
            "wideLayout": True,
        },
    }


def derived_stat(panel_id, title, signal, grid, *, unit=None, description="",
                 decimals=None):
    panel = stat(panel_id, title, signal, grid, unit=unit,
                 description=description, decimals=decimals)
    panel["targets"] = [_target(_derived_query(
        f'r.signal == "{signal}"', latest=True))]
    return panel


def derived_timeseries(panel_id, title, signals, grid, *, unit=None,
                       description="", decimals=None):
    condition = " or ".join(f'r.signal == "{signal}"' for signal in signals)
    panel = timeseries(panel_id, title, condition, grid, unit=unit,
                       description=description, decimals=decimals)
    panel["targets"] = [_target(_derived_query(condition, aggregate=True))]
    return panel


def transform_events(panel_id, grid):
    query = (
        'from(bucket: "home")\n'
        '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        '  |> filter(fn: (r) => r._measurement == "telemetry_event" and '
        'r._field == "message")\n'
        '  |> keep(columns: ["_time", "severity", "rule", "signal", "_value", '
        '"session"])\n'
        '  |> group()\n'
        '  |> rename(columns: {_time: "time", _value: "message"})\n'
        '  |> sort(columns: ["time"], desc: true)\n'
        '  |> limit(n: 100)'
    )
    return {
        "id": panel_id,
        "title": "Transform diagnostics",
        "type": "table",
        "description": (
            "Timestamp-based diagnostic events. These are engineering aids, not "
            "vehicle safety interlocks or validated alarms."
        ),
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(query, "table")],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"cellHeight": "sm", "showHeader": True},
    }


def gg_plot(panel_id, grid):
    query = (
        'from(bucket: "home")\n'
        '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        '  |> filter(fn: (r) => r._measurement == "vehicle_dynamics" and '
        '(r._field == "longitudinal_g" or r._field == "lateral_g"))\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n'
        '  |> group(columns: ["car", "session"])\n'
        '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> keep(columns: ["_time", "longitudinal_g", "lateral_g", "session"])'
    )
    return {
        "id": panel_id,
        "title": "GG plot",
        "type": "xychart",
        "description": (
            "Lateral versus longitudinal acceleration. Points appear only while the "
            "CAN9 IMU reports valid data and a recent accepted alignment state."
        ),
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(query, "table")],
        "fieldConfig": {
            "defaults": {
                "unit": "accG",
                "color": {"mode": "palette-classic"},
                "custom": {
                    "show": "points", "pointSize": {"fixed": 3},
                    "axisPlacement": "auto", "axisLabel": "g",
                    "axisColorMode": "text", "axisBorderShow": True,
                    "axisCenteredZero": True,
                    "scaleDistribution": {"type": "linear"},
                    "hideFrom": {"tooltip": False, "viz": False, "legend": False},
                },
            },
            "overrides": [],
        },
        "options": {
            "seriesMapping": "manual",
            "series": [{"pointColor": {}, "x": "lateral_g", "y": "longitudinal_g"}],
            "dims": {"x": "lateral_g"},
            "tooltip": {"mode": "single", "sort": "none"},
            "legend": {"showLegend": False, "displayMode": "list",
                       "placement": "bottom", "calcs": []},
        },
    }


def live_can_status(panel_id, grid):
    query = (
        'import "array"\n\n'
        'recent = from(bucket: "home")\n'
        '  |> range(start: -5s)\n'
        '  |> filter(fn: (r) => r._measurement == "can_frame" and r._field == "seq")\n'
        '  |> group()\n'
        '  |> last()\n'
        '  |> map(fn: (r) => ({_time: now(), _measurement: "telemetry_status", '
        '_field: "online", _value: 1}))\n\n'
        'fallback = array.from(rows: [{_time: now(), _measurement: "telemetry_status", '
        '_field: "online", _value: 0}])\n\n'
        'union(tables: [fallback, recent])\n'
        '  |> group()\n'
        '  |> max(column: "_value")'
    )
    return {
        "id": panel_id,
        "title": "Live CAN telemetry",
        "type": "stat",
        "description": (
            "LIVE means the telemetry Pi received CAN data within the last five seconds. "
            "This is a read-only status indicator, not a vehicle control."
        ),
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(query)],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": [{
                    "type": "value",
                    "options": {
                        "0": {"color": "red", "index": 0,
                              "text": "OFFLINE / NO RECENT CAN"},
                        "1": {"color": "green", "index": 1, "text": "LIVE"},
                    },
                }],
                "noValue": "OFFLINE / NO RECENT CAN",
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "red", "value": None},
                              {"color": "green", "value": 1}],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "wideLayout": True,
        },
    }


def latest_table(panel_id, title, condition, grid, *, dbc=None, description=""):
    return {
        "id": panel_id,
        "title": title,
        "type": "table",
        "description": description,
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(
            _signal_query(condition, dbc=dbc, latest=True, table=True), "table"
        )],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"cellHeight": "sm", "showHeader": True},
    }


def bar_gauge(panel_id, title, condition, grid, *, dbc=None, unit=None,
              description="", minimum=None, maximum=None, decimals=None):
    return {
        "id": panel_id,
        "title": title,
        "type": "bargauge",
        "description": description,
        "gridPos": grid,
        "datasource": DATASOURCE,
        "targets": [_target(_signal_query(condition, dbc=dbc, latest=True))],
        "fieldConfig": {
            "defaults": _field_defaults(
                unit, minimum=minimum, maximum=maximum, decimals=decimals
            ),
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient",
            "minVizHeight": 10,
            "minVizWidth": 0,
            "namePlacement": "auto",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
        },
    }


def _dashboard(uid, title, description, panels, *, variables=None):
    return {
        "id": None,
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["trevcan", "off-car", "read-only"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "panels": panels,
        "time": {"from": "now-5m", "to": "now"},
        "timepicker": {
            "refresh_intervals": list(REFRESH_INTERVALS),
            "quick_ranges": [dict(item) for item in QUICK_RANGES],
        },
        "templating": {"list": variables or []},
        "annotations": {"list": []},
        "refresh": "5s",
        "schemaVersion": 39,
        "version": 1,
        "links": [
            {
                "asDropdown": False,
                "icon": "external link",
                "includeVars": False,
                "keepTime": False,
                "tags": [],
                "targetBlank": True,
                "title": "Telemetry tools",
                "tooltip": "Start or end a supervised vehicle telemetry test",
                "type": "link",
                "url": "http://192.168.0.136:8080",
            }
        ],
    }


def _custom_variable(name, label, values, current):
    return {
        "name": name,
        "label": label,
        "type": "custom",
        "query": ",".join(values),
        "current": {"selected": False, "text": current, "value": current},
        "options": [
            {"selected": value == current, "text": value, "value": value}
            for value in values
        ],
        "hide": 0,
        "includeAll": False,
        "multi": False,
        "skipUrlSync": False,
    }


def overview_dashboard():
    panels = [
        stat(1, "VCU speed", "VCU_Speed_MPH", {"x": 0, "y": 0, "w": 4, "h": 4},
             dbc="VCU.dbc", unit="velocitymph", decimals=1),
        stat(2, "APPS", "VCU_APPS_Value", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc="VCU.dbc", unit="percent", minimum=0, maximum=100, decimals=1),
        stat(3, "Brake pressure", "VCU_BSE_PSI", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc="VCU.dbc", unit="suffix:PSI", minimum=0, decimals=1),
        stat(4, "HVC state", "HVC_BMS_State", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc="hvc.dbc", decimals=0),
        stat(5, "State of charge", "HVC_SOC_Percent", {"x": 16, "y": 0, "w": 4, "h": 4},
             dbc="hvc.dbc", unit="percent", minimum=0, maximum=100, decimals=1),
        stat(6, "Pack voltage", "HVC_Batt_Voltage_V", {"x": 20, "y": 0, "w": 4, "h": 4},
             dbc="hvc.dbc", unit="volt", decimals=1),
        timeseries(
            7, "Motor speed", 'r.signal == "INV_Motor_Speed"',
            {"x": 0, "y": 4, "w": 12, "h": 8}, unit="suffix:RPM",
            description="Inverter motor speed. Vehicle speed is shown separately in mph.",
        ),
        timeseries(
            8, "Accelerator pedal position",
            'r.signal == "VCU_APPS1_Value" or r.signal == "VCU_APPS2_Value" or r.signal == "VCU_APPS_Value"',
            {"x": 12, "y": 4, "w": 12, "h": 8}, dbc="VCU.dbc",
            unit="percent", minimum=0, maximum=100,
        ),
        timeseries(
            9, "Inverter torque",
            'r.signal == "INV_Commanded_Torque" or r.signal == "INV_Torque_Feedback"',
            {"x": 0, "y": 12, "w": 8, "h": 8}, dbc="BMS-Inverter-Only.dbc",
            unit="suffix:Nm",
        ),
        timeseries(
            10, "HVC bus voltage",
            'r.signal == "HVC_Batt_Voltage_V" or r.signal == "HVC_Inv_Voltage_V"',
            {"x": 8, "y": 12, "w": 8, "h": 8}, dbc="hvc.dbc",
            unit="volt",
        ),
        timeseries(
            11, "HVC pack current",
            'r.signal == "HVC_Current_Low_A" or r.signal == "HVC_Current_High_A" or r.signal == "HVC_Pack_Current_A"',
            {"x": 16, "y": 12, "w": 8, "h": 8}, dbc="hvc.dbc",
            unit="amp",
        ),
        timeseries(
            12, "Key temperatures",
            'r.signal == "INV_Motor_Temp" or r.signal == "INV_Coolant_Temp" or r.signal == "INV_Hot_Spot_Temp_Inverter" or r.signal == "HVC_Acc_Temp_Min_C" or r.signal == "HVC_Acc_Temp_Max_C"',
            {"x": 0, "y": 20, "w": 12, "h": 8}, unit="suffix:°C",
        ),
        timeseries(
            13, "Low-voltage system currents",
            'r.signal == "LV_Current" or r.signal == "HC_Current"',
            {"x": 12, "y": 20, "w": 12, "h": 8}, dbc="Baby_MOBO.dbc",
            unit="suffix:A",
        ),
    ]
    for panel in panels:
        panel["gridPos"]["y"] += 3
    panels.insert(0, live_can_status(14, {"x": 0, "y": 0, "w": 24, "h": 3}))
    return _dashboard(
        "trevcan-overview", "TREVCAN - Vehicle Overview",
        "Readable high-level vehicle telemetry. Each graph keeps compatible units together.",
        panels,
    )


def inverter_dashboard():
    dbc = "BMS-Inverter-Only.dbc"
    panels = [
        stat(1, "Motor speed", "INV_Motor_Speed", {"x": 0, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:RPM", decimals=0),
        stat(2, "Commanded torque", "INV_Commanded_Torque", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:Nm", decimals=1),
        stat(3, "Torque feedback", "INV_Torque_Feedback", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:Nm", decimals=1),
        stat(4, "DC bus voltage", "INV_DC_Bus_Voltage", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:V", decimals=1),
        stat(5, "DC bus current", "INV_DC_Bus_Current", {"x": 16, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:A", decimals=1),
        stat(6, "Inverter state", "INV_Inverter_State", {"x": 20, "y": 0, "w": 4, "h": 4},
             dbc=dbc, decimals=0),
        timeseries(7, "Motor speed", 'r.signal == "INV_Motor_Speed"',
                   {"x": 0, "y": 4, "w": 8, "h": 8}, dbc=dbc, unit="suffix:RPM"),
        timeseries(8, "Torque command and feedback",
                   'r.signal == "INV_Commanded_Torque" or r.signal == "INV_Torque_Feedback"',
                   {"x": 8, "y": 4, "w": 8, "h": 8}, dbc=dbc, unit="suffix:Nm"),
        timeseries(9, "DC bus voltage", 'r.signal == "INV_DC_Bus_Voltage"',
                   {"x": 16, "y": 4, "w": 8, "h": 8}, dbc=dbc, unit="suffix:V"),
        timeseries(10, "Phase and DC currents",
                   'r.signal == "INV_Phase_A_Current" or r.signal == "INV_Phase_B_Current" or r.signal == "INV_Phase_C_Current" or r.signal == "INV_DC_Bus_Current"',
                   {"x": 0, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="suffix:A"),
        timeseries(11, "Power electronics temperatures",
                   'r.signal == "INV_Module_A_Temp" or r.signal == "INV_Module_B_Temp" or r.signal == "INV_Module_C_Temp" or r.signal == "INV_GDB_Temp" or r.signal == "INV_Control_Board_Temp"',
                   {"x": 12, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="suffix:°C"),
        timeseries(12, "Motor and coolant temperatures",
                   'r.signal == "INV_Motor_Temp" or r.signal == "INV_Coolant_Temp" or r.signal == "INV_Hot_Spot_Temp_Motor" or r.signal == "INV_Hot_Spot_Temp_Inverter" or r.signal == "INV_RTD1_Temperature" or r.signal == "INV_RTD2_Temperature"',
                   {"x": 0, "y": 20, "w": 12, "h": 8}, dbc=dbc, unit="suffix:°C"),
        latest_table(13, "Limits and fault words",
                     'r.signal =~ /^INV_(BMS_Limit|Limit_|Low_Speed_Limit|Enable_Lockout|Post_Fault|Run_Fault)/',
                     {"x": 12, "y": 20, "w": 12, "h": 8}, dbc=dbc,
                     description="Latest read-only limit flags and fault words."),
    ]
    return _dashboard(
        "trevcan-inverter", "TREVCAN - Inverter",
        "Read-only replacement for the monitoring portion of the React inverter dashboard.",
        panels,
    )


def hvc_dashboard():
    dbc = "hvc.dbc"
    panels = [
        stat(1, "State of charge", "HVC_SOC_Percent", {"x": 0, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="percent", minimum=0, maximum=100, decimals=1),
        stat(2, "Battery voltage", "HVC_Batt_Voltage_V", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="volt", decimals=1),
        stat(3, "Inverter voltage", "HVC_Inv_Voltage_V", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="volt", decimals=1),
        stat(4, "Pack current", "HVC_Pack_Current_A", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="amp", decimals=1),
        stat(5, "BMS state", "HVC_BMS_State", {"x": 16, "y": 0, "w": 4, "h": 4},
             dbc=dbc, decimals=0),
        stat(6, "SDC open", "HVC_SDC_Open", {"x": 20, "y": 0, "w": 4, "h": 4},
             dbc=dbc, decimals=0),
        timeseries(7, "Battery and inverter voltage",
                   'r.signal == "HVC_Batt_Voltage_V" or r.signal == "HVC_Inv_Voltage_V"',
                   {"x": 0, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="volt"),
        timeseries(8, "Current sensor channels",
                   'r.signal == "HVC_Current_Low_A" or r.signal == "HVC_Current_High_A" or r.signal == "HVC_Pack_Current_A"',
                   {"x": 12, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="amp"),
        timeseries(9, "Accumulator voltage extremes",
                   'r.signal == "HVC_Acc_Volt_Min_mV" or r.signal == "HVC_Acc_Volt_Max_mV"',
                   {"x": 0, "y": 12, "w": 8, "h": 8}, dbc=dbc, unit="suffix:mV"),
        timeseries(10, "Accumulator temperature extremes",
                   'r.signal == "HVC_Acc_Temp_Min_C" or r.signal == "HVC_Acc_Temp_Max_C"',
                   {"x": 8, "y": 12, "w": 8, "h": 8}, dbc=dbc, unit="suffix:°C"),
        timeseries(11, "State of charge", 'r.signal == "HVC_SOC_Percent"',
                   {"x": 16, "y": 12, "w": 8, "h": 8}, dbc=dbc,
                   unit="percent", minimum=0, maximum=100),
        timeseries(12, "Current limits",
                   'r.signal == "HVC_Negative_Current_Limit_mA" or r.signal == "HVC_Positive_Current_Limit_mA"',
                   {"x": 0, "y": 20, "w": 12, "h": 8}, dbc=dbc, unit="suffix:mA"),
        timeseries(13, "HVC reference temperature", 'r.signal == "HVC_Ref_Temp_C"',
                   {"x": 12, "y": 20, "w": 12, "h": 8}, dbc=dbc, unit="suffix:°C"),
        latest_table(14, "Safety and fault status",
                     'r.signal == "HVC_IMD_Fault" or r.signal == "HVC_BMS_Fault" or r.signal == "HVC_SDC_Open" or r.signal =~ /^HVC_Err_/ or r.signal == "HVC_PL_Signal_Reason"',
                     {"x": 0, "y": 28, "w": 24, "h": 8}, dbc=dbc),
    ]
    return _dashboard(
        "trevcan-hvc", "TREVCAN - HVC",
        "Read-only HVC electrical, accumulator, safety, and fault telemetry.", panels,
    )


def vcu_dashboard():
    dbc = "VCU.dbc"
    panels = [
        stat(1, "VCU state", "VCU_State", {"x": 0, "y": 0, "w": 4, "h": 4}, dbc=dbc, decimals=0),
        stat(2, "Speed", "VCU_Speed_MPH", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="velocitymph", decimals=1),
        stat(3, "APPS", "VCU_APPS_Value", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="percent", minimum=0, maximum=100, decimals=1),
        stat(4, "Brake pressure", "VCU_BSE_PSI", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:PSI", minimum=0, decimals=1),
        stat(5, "Ready to drive", "VCU_RTD_Active", {"x": 16, "y": 0, "w": 4, "h": 4}, dbc=dbc, decimals=0),
        stat(6, "Red car", "VCU_Red_Car", {"x": 20, "y": 0, "w": 4, "h": 4}, dbc=dbc, decimals=0),
        timeseries(7, "Accelerator pedal positions",
                   'r.signal == "VCU_APPS1_Value" or r.signal == "VCU_APPS2_Value" or r.signal == "VCU_APPS_Value"',
                   {"x": 0, "y": 4, "w": 12, "h": 8}, dbc=dbc,
                   unit="percent", minimum=0, maximum=100),
        timeseries(8, "Brake pressure", 'r.signal == "VCU_BSE_PSI"',
                   {"x": 12, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="suffix:PSI", minimum=0),
        timeseries(9, "APPS sensor voltages",
                   'r.signal == "VCU_APPS1_Filt_mV" or r.signal == "VCU_APPS1_Raw_mV" or r.signal == "VCU_APPS2_Filt_mV" or r.signal == "VCU_APPS2_Raw_mV"',
                   {"x": 0, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="suffix:V"),
        timeseries(10, "Brake sensor voltages",
                   'r.signal == "VCU_BSE_Filt_mV" or r.signal == "VCU_BSE_Raw_mV"',
                   {"x": 12, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="suffix:V"),
        latest_table(11, "Readiness and shutdown-circuit status",
                     'r.signal == "VCU_RTD_Active" or r.signal == "VCU_Red_Car" or r.signal =~ /^VCU_Dead_/',
                     {"x": 0, "y": 20, "w": 8, "h": 8}, dbc=dbc),
        latest_table(12, "Pedal health",
                     'r.signal =~ /^VCU_APPS.*(Valid|Stale|Err|Range|Implausible)$/ or r.signal =~ /^VCU_BSE_.*(Valid|Stale|Err|Range)$/',
                     {"x": 8, "y": 20, "w": 8, "h": 8}, dbc=dbc),
        latest_table(13, "CAN health",
                     'r.signal =~ /^VCU_(Controls|DAQ)_/ or r.signal == "VCU_Health_Heartbeat"',
                     {"x": 16, "y": 20, "w": 8, "h": 8}, dbc=dbc),
        latest_table(14, "Current VCU configuration (read-only)",
                     'r.message == "VCU_Config"',
                     {"x": 0, "y": 28, "w": 24, "h": 8}, dbc=dbc,
                     description="Grafana displays configuration feedback but does not transmit configuration commands."),
    ]
    return _dashboard(
        "trevcan-vcu", "TREVCAN - VCU",
        "Read-only VCU state, pedals, sensor voltages, readiness, and CAN health.", panels,
    )


def mobo_dashboard():
    dbc = "Baby_MOBO.dbc"
    panels = [
        stat(1, "System state", "System_State", {"x": 0, "y": 0, "w": 4, "h": 4}, dbc=dbc, decimals=0),
        stat(2, "Battery voltage", "Battery_Voltage", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:V", decimals=2),
        stat(3, "5 V sense", "FiveV_Sense", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:V", decimals=2),
        stat(4, "Rear brake pressure", "BSE_PSI_Rear", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:PSI", decimals=1),
        stat(5, "LV current", "LV_Current", {"x": 16, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:A", decimals=2),
        stat(6, "HC current", "HC_Current", {"x": 20, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:A", decimals=2),
        timeseries(7, "Power rails",
                   'r.signal == "Battery_Voltage" or r.signal == "FiveV_Sense"',
                   {"x": 0, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="suffix:V"),
        timeseries(8, "Current telemetry",
                   'r.signal == "LV_Current" or r.signal == "HC_Current" or r.signal == "LV_Current_Peak" or r.signal == "HC_Current_Peak"',
                   {"x": 12, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="suffix:A"),
        timeseries(9, "Rear brake pressure", 'r.signal == "BSE_PSI_Rear"',
                   {"x": 0, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="suffix:PSI"),
        timeseries(10, "CAN counters",
                   'r.message == "MOBO_CAN_Stats"',
                   {"x": 12, "y": 12, "w": 12, "h": 8}, dbc=dbc, unit="short"),
        latest_table(11, "Safety inputs",
                     'r.signal =~ /^(SDC[123]|BMS|BSPD|IMD)_(Raw|Debounced|Latched)$/',
                     {"x": 0, "y": 20, "w": 12, "h": 10}, dbc=dbc),
        latest_table(12, "Relay state (read-only)",
                     'r.message == "MOBO_Relay_Status"',
                     {"x": 12, "y": 20, "w": 12, "h": 10}, dbc=dbc,
                     description="Displays commanded and actual relay feedback; Grafana does not send relay commands."),
        latest_table(13, "Errors and warnings",
                     'r.message == "MOBO_Errors" or r.signal == "Fault_Count" or r.signal == "Error_Summary" or r.signal == "Has_Warnings"',
                     {"x": 0, "y": 30, "w": 24, "h": 8}, dbc=dbc),
    ]
    return _dashboard(
        "trevcan-mobo", "TREVCAN - MOBO",
        "Read-only MOBO power, current, safety, relay-feedback, and CAN telemetry.", panels,
    )


def bms_dashboard():
    dbc = "BMS-Firmware-RTOS-Complete.dbc"
    module_filter = 'r.message =~ /_${bms_module}$/'
    panels = [
        stat(1, "Module state", "BMS_State", {"x": 0, "y": 0, "w": 4, "h": 4},
             dbc=dbc, decimals=0, extra_condition=module_filter,
             description="Selected by the BMS Module control above."),
        stat(2, "Fault count", "Fault_Count", {"x": 4, "y": 0, "w": 4, "h": 4},
             dbc=dbc, decimals=0, extra_condition=module_filter),
        stat(3, "Minimum cell temperature", "Min_Cell_Temp", {"x": 8, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:°C", decimals=1, extra_condition=module_filter),
        stat(4, "Maximum cell temperature", "Max_Cell_Temp", {"x": 12, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:°C", decimals=1, extra_condition=module_filter),
        stat(5, "BMS1 stack voltage", "BMS1_Stack_Voltage", {"x": 16, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:V", decimals=2, extra_condition=module_filter),
        stat(6, "BMS2 stack voltage", "BMS2_Stack_Voltage", {"x": 20, "y": 0, "w": 4, "h": 4},
             dbc=dbc, unit="suffix:V", decimals=2, extra_condition=module_filter),
        timeseries(7, "Cell-voltage summary",
                   f'({module_filter}) and (r.signal == "BMS1_Voltage_Average" or r.signal == "BMS1_Voltage_Min" or r.signal == "BMS1_Voltage_Max" or r.signal == "BMS2_Voltage_Average" or r.signal == "BMS2_Voltage_Min" or r.signal == "BMS2_Voltage_Max")',
                   {"x": 0, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="suffix:mV"),
        timeseries(8, "Cell-temperature summary",
                   f'({module_filter}) and (r.signal == "Min_Cell_Temp" or r.signal == "Max_Cell_Temp" or r.signal == "Avg_Cell_Temp")',
                   {"x": 12, "y": 4, "w": 12, "h": 8}, dbc=dbc, unit="suffix:°C"),
        bar_gauge(9, "Latest cell voltages - selected module",
                  'r.signal =~ /^CellVoltage_m${bms_module}_cellgrp([1-9]|1[0-8])$/',
                  {"x": 0, "y": 12, "w": 12, "h": 14}, dbc=dbc, unit="suffix:mV",
                  minimum=2500, maximum=4500, decimals=0,
                  description="Eighteen cells only; use the module selector instead of plotting all 108 cells together."),
        timeseries(10, "Cell-voltage history - selected module",
                   'r.signal =~ /^CellVoltage_m${bms_module}_cellgrp([1-9]|1[0-8])$/',
                   {"x": 12, "y": 12, "w": 12, "h": 14}, dbc=dbc, unit="suffix:mV",
                   minimum=2500, maximum=4500),
        latest_table(11, "Latest thermistors - selected module",
                     'r.signal =~ /^(Temp_m${bms_module}_cellgrp|AmbientTemp_m${bms_module}_)/',
                     {"x": 0, "y": 26, "w": 12, "h": 14}, dbc=dbc,
                     description="A sorted last-value table avoids overlaying 56 thermistor traces."),
        timeseries(12, "E-meter thermistors - selected module",
                   f'({module_filter}) and r.signal =~ /^E_Meter_Thermistor_[1-6]$/',
                   {"x": 12, "y": 26, "w": 12, "h": 8}, dbc=dbc, unit="suffix:°C"),
        timeseries(13, "Pack current sensors",
                   'r.signal == "LC_Current" or r.signal == "HC_Current"',
                   {"x": 12, "y": 34, "w": 12, "h": 6}, dbc=dbc, unit="suffix:A"),
        latest_table(14, "Heartbeat, warnings, and alarms - selected module",
                     f'({module_filter}) and (r.message =~ /^BMS_Heartbeat_/ or r.message =~ /^BMS[12]_Balance_Detail_/ or r.message =~ /^BMS_Chip_Status_/)',
                     {"x": 0, "y": 40, "w": 24, "h": 10}, dbc=dbc),
    ]
    module_variable = _custom_variable("bms_module", "BMS Module", [str(i) for i in range(6)], "0")
    return _dashboard(
        "trevcan-bms", "TREVCAN - BMS",
        "Module-selectable replacement for BMS overview, status, cell-voltage, and thermistor monitoring.",
        panels, variables=[module_variable],
    )


def analysis_dashboard():
    panels = [
        live_can_status(1, {"x": 0, "y": 0, "w": 24, "h": 3}),
        derived_stat(2, "Distance this test", "Distance_m",
                     {"x": 0, "y": 3, "w": 4, "h": 4}, unit="lengthm", decimals=1),
        derived_stat(3, "Energy used", "Energy_Used_Wh",
                     {"x": 4, "y": 3, "w": 4, "h": 4}, unit="watth", decimals=1,
                     description="Integrated discharge energy for the current test session."),
        derived_stat(4, "Energy charged", "Energy_Charged_Wh",
                     {"x": 8, "y": 3, "w": 4, "h": 4}, unit="watth", decimals=1,
                     description="Working convention: positive HVC pack current is charging."),
        derived_stat(5, "Net energy change", "Net_Energy_Change_Wh",
                     {"x": 12, "y": 3, "w": 4, "h": 4}, unit="watth", decimals=1),
        derived_stat(6, "Pack power", "Pack_Power_W",
                     {"x": 16, "y": 3, "w": 4, "h": 4}, unit="watt", decimals=0,
                     description="Positive is charging; negative is discharging."),
        derived_stat(7, "IMU alignment valid", "IMU_Alignment_Valid",
                     {"x": 20, "y": 3, "w": 4, "h": 4}, decimals=0,
                     description="GG data is suppressed unless this is 1."),
        derived_timeseries(
            8, "Vehicle speed", ["Vehicle_Speed_mps"],
            {"x": 0, "y": 7, "w": 12, "h": 8}, unit="velocityms",
            description="VCU speed converted from mph to m/s using source timestamps."),
        derived_timeseries(
            9, "Acceleration from vehicle speed", ["Vehicle_Accel_From_Speed_mps2"],
            {"x": 12, "y": 7, "w": 12, "h": 8}, unit="accMS2",
            description="Smoothed local slope of speed; intended for engineering analysis."),
        derived_timeseries(
            10, "Pack power", ["Pack_Power_W"],
            {"x": 0, "y": 15, "w": 12, "h": 8}, unit="watt",
            description="Computed from the nearest voltage/current pair within 200 ms."),
        derived_timeseries(
            11, "Session energy", ["Energy_Used_Wh", "Energy_Charged_Wh",
                                   "Net_Energy_Change_Wh"],
            {"x": 12, "y": 15, "w": 12, "h": 8}, unit="watth"),
        gg_plot(12, {"x": 0, "y": 23, "w": 12, "h": 10}),
        derived_timeseries(
            13, "Accumulator temperature rise rate",
            ["Accumulator_Temp_Rise_C_per_s"],
            {"x": 12, "y": 23, "w": 12, "h": 10}, unit="suffix:degC/s",
            description="Rolling timestamp-based slope of HVC maximum accumulator temperature."),
        transform_events(14, {"x": 0, "y": 33, "w": 24, "h": 8}),
    ]
    return _dashboard(
        "trevcan-live-analysis", "TREVCAN - Live Analysis",
        "Session-aware derived telemetry calculated on the telemetry Pi from source timestamps.",
        panels,
    )


def organized_dashboards():
    """Return stable filenames and dashboard documents for provisioning."""
    return [
        ("trevcan-overview.json", overview_dashboard()),
        ("trevcan-live-analysis.json", analysis_dashboard()),
        ("trevcan-bms.json", bms_dashboard()),
        ("trevcan-hvc.json", hvc_dashboard()),
        ("trevcan-inverter.json", inverter_dashboard()),
        ("trevcan-vcu.json", vcu_dashboard()),
        ("trevcan-mobo.json", mobo_dashboard()),
    ]
