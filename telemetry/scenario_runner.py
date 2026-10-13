"""Finite, DBC-encoded end-to-end scenarios for the off-car telemetry stack.

The runner deliberately never imports or opens python-can.  It encodes raw CAN
payloads from the repository DBC files, places them in an isolated temporary
car spool, and sends them through the normal authenticated TCP receiver.  This
exercises the decoder, transform worker, InfluxDB export, and Grafana queries
without transmitting anything onto a physical CAN bus.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .car import CarSpool, send_forever


DBC_DIRECTORY = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files"


@dataclass(frozen=True)
class ScenarioEvent:
    offset_seconds: float
    label: str
    frame: dict


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    events: tuple
    expected: tuple = ()
    forbidden: tuple = ()
    buffer_before_connect: bool = False


class FrameFactory:
    """Encode a small, intentional subset of the real DBC messages."""

    MESSAGE_FILES = {
        "VCU_Summary": ("VCU.dbc", "can1"),
        "HVC_IO_Current": ("hvc.dbc", "can0"),
        "HVC_IO_VSense": ("hvc.dbc", "can0"),
        "HVC_ACC_Summary": ("hvc.dbc", "can0"),
        "HVC_BMS_State": ("hvc.dbc", "can0"),
        "ImuAlign": ("can9-database-01.09.dbc", "can1"),
        "ImuData": ("can9-database-01.09.dbc", "can1"),
    }

    def __init__(self, dbc_directory=DBC_DIRECTORY):
        import cantools

        self.messages = {}
        databases = {}
        for message_name, (filename, bus) in self.MESSAGE_FILES.items():
            if filename not in databases:
                databases[filename] = cantools.database.load_file(
                    Path(dbc_directory) / filename, strict=False)
            self.messages[message_name] = (
                databases[filename].get_message_by_name(message_name), bus)

    @staticmethod
    def _default_value(signal):
        value = 0.0
        if signal.minimum is not None and value < signal.minimum:
            value = signal.minimum
        if signal.maximum is not None and value > signal.maximum:
            value = signal.maximum
        return value

    def encode(self, message_name, **overrides):
        message, bus = self.messages[message_name]
        values = {signal.name: self._default_value(signal)
                  for signal in message.signals}
        unknown = set(overrides) - set(values)
        if unknown:
            raise ValueError(
                f"unknown {message_name} signals: {', '.join(sorted(unknown))}")
        values.update(overrides)
        data = message.encode(values, strict=True)
        return {
            "bus": bus,
            "can_id": message.frame_id,
            "is_extended": bool(message.is_extended_frame),
            "is_remote": False,
            "is_fd": len(data) > 8,
            "is_error": False,
            "dlc": len(data),
            "data": data,
        }


def _event(factory, offset, label, message, **signals):
    return ScenarioEvent(offset, label, factory.encode(message, **signals))


def _shift(events, seconds):
    return tuple(ScenarioEvent(event.offset_seconds + seconds, event.label, event.frame)
                 for event in events)


def build_scenarios(factory=None):
    factory = factory or FrameFactory()
    speed = tuple(
        _event(factory, offset, f"speed {mph:g} mph", "VCU_Summary",
               VCU_Heartbeat=index, VCU_State=1, VCU_Speed_MPH=mph,
               VCU_HVC_Summary_Valid=1)
        for index, (offset, mph) in enumerate((
            (0.00, 0), (0.15, 5), (0.30, 20),
            (0.45, 35), (0.60, 20), (0.75, 0)), 1)
    )

    electrical = (
        _event(factory, 0.00, "400 V", "HVC_IO_VSense",
               HVC_Batt_Voltage_V=400, HVC_Inv_Voltage_V=398),
        _event(factory, 0.02, "+10 A charging", "HVC_IO_Current",
               HVC_Current_Low_A=10, HVC_Current_High_A=10,
               HVC_Pack_Current_A=10),
        _event(factory, 0.25, "400 V", "HVC_IO_VSense",
               HVC_Batt_Voltage_V=400, HVC_Inv_Voltage_V=399),
        _event(factory, 0.27, "+10 A charging", "HVC_IO_Current",
               HVC_Current_Low_A=10, HVC_Current_High_A=10,
               HVC_Pack_Current_A=10),
        _event(factory, 0.50, "398 V", "HVC_IO_VSense",
               HVC_Batt_Voltage_V=398, HVC_Inv_Voltage_V=397),
        _event(factory, 0.52, "-15 A discharging", "HVC_IO_Current",
               HVC_Current_Low_A=-15, HVC_Current_High_A=-15,
               HVC_Pack_Current_A=-15),
        _event(factory, 0.75, "398 V", "HVC_IO_VSense",
               HVC_Batt_Voltage_V=398, HVC_Inv_Voltage_V=397),
        _event(factory, 0.77, "-15 A discharging", "HVC_IO_Current",
               HVC_Current_Low_A=-15, HVC_Current_High_A=-15,
               HVC_Pack_Current_A=-15),
    )

    temperature_spike = (
        _event(factory, 0.00, "temperature baseline", "HVC_ACC_Summary",
               HVC_Acc_Volt_Min_mV=3500, HVC_Acc_Volt_Max_mV=3600,
               HVC_Acc_Temp_Min_C=24, HVC_Acc_Temp_Max_C=25),
        _event(factory, 0.25, "temperature steady", "HVC_ACC_Summary",
               HVC_Acc_Volt_Min_mV=3500, HVC_Acc_Volt_Max_mV=3600,
               HVC_Acc_Temp_Min_C=24.2, HVC_Acc_Temp_Max_C=25.2),
        _event(factory, 0.60, "temperature +21 C edge", "HVC_ACC_Summary",
               HVC_Acc_Volt_Min_mV=3500, HVC_Acc_Volt_Max_mV=3600,
               HVC_Acc_Temp_Min_C=45, HVC_Acc_Temp_Max_C=46),
        _event(factory, 0.80, "temperature warning cooldown", "HVC_ACC_Summary",
               HVC_Acc_Volt_Min_mV=3500, HVC_Acc_Volt_Max_mV=3600,
               HVC_Acc_Temp_Min_C=45.2, HVC_Acc_Temp_Max_C=46.2),
    )

    gg_valid = (
        _event(factory, 0.00, "fine IMU alignment", "ImuAlign",
               AlignStatus=3, AlignXYError=0, AlignZError=0, AlignError=0,
               AlignX=0, AlignY=0, AlignZ=0),
        _event(factory, 0.15, "forward/left acceleration", "ImuData",
               ImuValid=1, AccelerationX=4.875, AccelerationY=2.5,
               AccelerationZ=9.75),
        _event(factory, 0.35, "braking/right acceleration", "ImuData",
               ImuValid=1, AccelerationX=-3.0, AccelerationY=-2.0,
               AccelerationZ=9.75),
        _event(factory, 0.55, "cornering acceleration", "ImuData",
               ImuValid=1, AccelerationX=0.5, AccelerationY=5.0,
               AccelerationZ=9.75),
    )

    gg_invalid = (
        _event(factory, 0.00, "ongoing IMU alignment", "ImuAlign",
               AlignStatus=1, AlignXYError=0, AlignZError=0, AlignError=0),
        _event(factory, 0.20, "valid IMU sample but invalid alignment", "ImuData",
               ImuValid=1, AccelerationX=4.0, AccelerationY=2.0,
               AccelerationZ=9.75),
    )

    stale_electrical = (
        _event(factory, 0.00, "voltage without a fresh current", "HVC_IO_VSense",
               HVC_Batt_Voltage_V=400, HVC_Inv_Voltage_V=399),
        _event(factory, 0.40, "current arrives 400 ms later", "HVC_IO_Current",
               HVC_Current_Low_A=20, HVC_Current_High_A=20,
               HVC_Pack_Current_A=20),
    )

    hvc_faults = (
        _event(factory, 0.00, "nominal HVC state", "HVC_BMS_State",
               HVC_BMS_State=1, HVC_BMB_Fault_Module=255,
               HVC_BMB_Fault_State=0),
        _event(factory, 0.30, "synthetic HVC errors", "HVC_BMS_State",
               HVC_BMS_State=4, HVC_Err_RefOverTemp=1,
               HVC_Err_ModuleTimeout=1, HVC_BMB_Fault_Module=2,
               HVC_BMB_Fault_State=5),
    )

    current_message, _ = factory.messages["HVC_IO_Current"]
    decode_mismatch = (
        ScenarioEvent(0.00, "intentional 8-byte payload for a 6-byte HVC message", {
            "bus": "can0", "can_id": current_message.frame_id,
            "is_extended": bool(current_message.is_extended_frame),
            "is_remote": False, "is_fd": False, "is_error": False,
            "dlc": 8, "data": bytes(8),
        }),
    )

    all_events = (
        speed + _shift(electrical, 1.10) + _shift(temperature_spike, 2.20) +
        _shift(gg_valid, 3.35) + _shift(hvc_faults, 4.25) +
        _shift(decode_mismatch, 4.90)
    )

    return {
        "speed-step": Scenario(
            "speed-step", "Unit conversion, distance integration, and speed derivative.",
            speed,
            ("Vehicle_Speed_mps", "Distance_m", "Vehicle_Accel_From_Speed_mps2")),
        "electrical-polarity": Scenario(
            "electrical-polarity",
            "Positive charging then negative discharging current and paired power/energy.",
            electrical,
            ("Pack_Power_W", "Charge_Added_Ah", "Charge_Removed_Ah",
             "Energy_Charged_Wh", "Energy_Used_Wh")),
        "temperature-spike": Scenario(
            "temperature-spike", "A 21 C jump inside one second; emits one warning.",
            temperature_spike,
            ("Accumulator_Temp_Rise_C_per_s", "temperature_spike")),
        "gg-valid": Scenario(
            "gg-valid", "Fine alignment plus valid CAN9 IMU samples; emits GG points.",
            gg_valid,
            ("IMU_Alignment_Valid", "Longitudinal_Accel_g", "Lateral_Accel_g")),
        "gg-invalid": Scenario(
            "gg-invalid", "Ongoing alignment suppresses otherwise-valid IMU GG points.",
            gg_invalid, ("IMU_Alignment_Valid",),
            ("Longitudinal_Accel_g", "Lateral_Accel_g")),
        "stale-electrical-pair": Scenario(
            "stale-electrical-pair",
            "Voltage/current separated by 400 ms; the 200 ms pairing gate rejects power.",
            stale_electrical, ("HVC_Batt_Voltage_V", "HVC_Pack_Current_A"),
            ("Pack_Power_W",)),
        "hvc-faults": Scenario(
            "hvc-faults", "Synthetic HVC error bits for the read-only fault table.",
            hvc_faults, ("HVC_Err_RefOverTemp", "HVC_Err_ModuleTimeout")),
        "decode-mismatch": Scenario(
            "decode-mismatch",
            "Valid raw CAN frame whose DLC disagrees with the DBC; remains raw/undecoded.",
            decode_mismatch, ("can_frame",), ("HVC_Pack_Current_A",)),
        "spool-drain": Scenario(
            "spool-drain",
            "Capture the speed scenario before connecting, then drain the durable spool.",
            speed, ("Vehicle_Speed_mps", "Distance_m"), buffer_before_connect=True),
        "all": Scenario(
            "all", "Run the positive-path calculations, one warning, faults, and DLC mismatch.",
            all_events,
            ("Vehicle_Speed_mps", "Distance_m", "Pack_Power_W",
             "Energy_Charged_Wh", "Energy_Used_Wh",
             "Accumulator_Temp_Rise_C_per_s", "temperature_spike",
             "IMU_Alignment_Valid", "Longitudinal_Accel_g",
             "HVC_Err_RefOverTemp", "can_frame")),
    }


def _append_event(spool, event):
    frame = event.frame
    spool.append(
        frame["can_id"], frame["data"], bus=frame["bus"],
        is_extended=frame["is_extended"], is_remote=frame["is_remote"],
        is_fd=frame["is_fd"], is_error=frame["is_error"], dlc=frame["dlc"],
        timestamp_ns=time.time_ns())


async def _capture_events(spool, scenario):
    start = asyncio.get_running_loop().time()
    for event in sorted(scenario.events, key=lambda item: item.offset_seconds):
        delay = start + event.offset_seconds - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
        _append_event(spool, event)
        print(f"[scenario] {event.offset_seconds:5.2f}s  {event.label}", flush=True)


async def _wait_for_drain(spool, sender, timeout_seconds):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while spool.status()["queued"]:
        if sender.done():
            await sender
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"sender did not drain {spool.status()['queued']} queued frames; "
                "check receiver status, address, and TELEMETRY_TOKEN")
        await asyncio.sleep(0.05)


async def run_scenario(scenario, spool, host, port, token, timeout_seconds):
    stop = asyncio.Event()
    sender = None
    try:
        if scenario.buffer_before_connect:
            await _capture_events(spool, scenario)
            print(f"[scenario] network held; queued={spool.status()['queued']}", flush=True)
            await asyncio.sleep(1)
        else:
            sender = asyncio.create_task(
                send_forever(spool, host, port, token, stop, window_size=64))
            await _capture_events(spool, scenario)
        if sender is None:
            sender = asyncio.create_task(
                send_forever(spool, host, port, token, stop, window_size=64))
        await _wait_for_drain(spool, sender, timeout_seconds)
        print("[scenario] receiver acknowledged every synthetic frame", flush=True)
    finally:
        stop.set()
        if sender is not None:
            await asyncio.wait_for(sender, 3)


def _query_influx(host, port, token, org, bucket, car_id):
    flux = (
        f"from(bucket: {json.dumps(bucket)})\n"
        "  |> range(start: -10m)\n"
        f"  |> filter(fn: (r) => r.car == {json.dumps(car_id)})\n"
        "  |> keep(columns: [\"_measurement\", \"_field\", \"_value\", "
        "\"signal\", \"rule\", \"severity\", \"message\", \"dbc\"])"
    )
    url = (f"http://{host}:{port}/api/v2/query?" +
           urllib.parse.urlencode({"org": org}))
    request = urllib.request.Request(
        url, data=flux.encode("utf-8"), method="POST",
        headers={"Authorization": f"Token {token}",
                 "Content-Type": "application/vnd.flux", "Accept": "text/csv"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


async def verify_scenario(scenario, car_id, host, port, token, org, bucket,
                          timeout_seconds):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    body = ""
    while asyncio.get_running_loop().time() < deadline:
        body = await asyncio.to_thread(
            _query_influx, host, port, token, org, bucket, car_id)
        if all(item in body for item in scenario.expected):
            break
        await asyncio.sleep(0.25)
    missing = [item for item in scenario.expected if item not in body]
    unexpected = [item for item in scenario.forbidden if item in body]
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise RuntimeError("Influx verification failed (" + "; ".join(details) + ")")
    print("[verify] expected Influx measurements/signals found", flush=True)
    if scenario.forbidden:
        print("[verify] suppression checks passed: " +
              ", ".join(scenario.forbidden), flush=True)


def _print_scenarios(scenarios):
    width = max(len(name) for name in scenarios)
    for name, scenario in scenarios.items():
        print(f"{name:<{width}}  {scenario.description}")


def main():
    parser = argparse.ArgumentParser(
        description="Send finite synthetic edge-case scenarios without touching SocketCAN")
    parser.add_argument("--scenario", default="all",
                        help="scenario name (default: all)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--host", default="127.0.0.1", help="telemetry receiver host")
    parser.add_argument("--port", type=int, default=8765, help="telemetry receiver port")
    parser.add_argument("--token", default=os.getenv("TELEMETRY_TOKEN"),
                        help="receiver token (default: TELEMETRY_TOKEN)")
    parser.add_argument("--car-id", help="test stream ID; default is unique per run")
    parser.add_argument("--db", help="keep/use this scenario spool instead of a temporary file")
    parser.add_argument("--timeout", type=float, default=15,
                        help="seconds allowed for sender drain and verification")
    parser.add_argument("--verify", action="store_true",
                        help="verify expected and suppressed output in local InfluxDB")
    parser.add_argument("--influx-host", default="127.0.0.1")
    parser.add_argument("--influx-port", type=int, default=8086)
    parser.add_argument("--influx-token", default=os.getenv("INFLUXDB_TOKEN"))
    parser.add_argument("--org", default="docs")
    parser.add_argument("--bucket", default="home")
    args = parser.parse_args()

    try:
        scenarios = build_scenarios()
    except (OSError, ImportError, ValueError) as exc:
        parser.error(f"cannot load scenario DBCs: {exc}")
    if args.list:
        _print_scenarios(scenarios)
        return
    if args.scenario not in scenarios:
        parser.error(f"unknown scenario {args.scenario!r}; use --list")
    if not args.token:
        parser.error("TELEMETRY_TOKEN is required")
    if args.verify and not args.influx_token:
        parser.error("INFLUXDB_TOKEN is required with --verify")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    scenario = scenarios[args.scenario]
    car_id = args.car_id or (
        f"scenario-{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
    temporary = None
    if args.db:
        spool_path = Path(args.db)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="trevcan-scenario-")
        spool_path = Path(temporary.name) / "scenario.sqlite3"

    print(json.dumps({"scenario": scenario.name, "car_id": car_id,
                      "frames": len(scenario.events), "receiver": f"{args.host}:{args.port}",
                      "socketcan": "not used"}, indent=2), flush=True)
    spool = None
    try:
        spool = CarSpool(spool_path, car_id)
        asyncio.run(run_scenario(
            scenario, spool, args.host, args.port, args.token, args.timeout))
        if args.verify:
            asyncio.run(verify_scenario(
                scenario, car_id, args.influx_host, args.influx_port,
                args.influx_token, args.org, args.bucket, args.timeout))
        print(f"PASS: {scenario.name} ({car_id})", flush=True)
    except (OSError, TimeoutError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"FAIL: {scenario.name}: {exc}\n")
    finally:
        if spool is not None:
            spool.close()
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
