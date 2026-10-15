"""Policy-driven live telemetry transforms for the telemetry Pi.

The transform engine consumes decoded frames in source-timestamp order.  It
calculates from every selected update, while rate-limiting only the points sent
to InfluxDB. ``prepare`` never mutates committed state: callers apply the
returned state only after the matching Influx write succeeds. The active
receiver keeps this state in RAM; process restarts reset running totals.
"""

from __future__ import annotations

import copy
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _escape_tag(value):
    return (str(value).replace("\\", "\\\\").replace(" ", "\\ ")
            .replace(",", "\\,").replace("=", "\\="))


def _escape_field(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _numeric(value):
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


class SessionRegistry:
    """Small atomically-written catalog shared by Test Controls and transforms."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self._cached_payload = None
        self._cached_mtime_ns = None

    def _read(self):
        if self.path is None or not self.path.exists():
            payload = {"version": 1, "sessions": []}
            self._cached_payload = payload
            self._cached_mtime_ns = None
            return copy.deepcopy(payload)
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if (self._cached_payload is not None and
                mtime_ns == self._cached_mtime_ns):
            return copy.deepcopy(self._cached_payload)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {"version": 1, "sessions": []}
        if payload.get("version") != 1 or not isinstance(payload.get("sessions"), list):
            return {"version": 1, "sessions": []}
        self._cached_payload = payload
        self._cached_mtime_ns = mtime_ns
        return copy.deepcopy(payload)

    def _write(self, payload):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self._cached_payload = copy.deepcopy(payload)
            self._cached_mtime_ns = self.path.stat().st_mtime_ns
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def active(self):
        for session in reversed(self._read()["sessions"]):
            if session.get("ended_ns") is None:
                return dict(session)
        return None

    def start(self, *, now_ns=None, force_new=False):
        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        payload = self._read()
        active = next((session for session in reversed(payload["sessions"])
                       if session.get("ended_ns") is None), None)
        if active is not None and not force_new:
            return dict(active)
        if active is not None:
            active["ended_ns"] = now_ns
        stamp = datetime.fromtimestamp(now_ns / 1_000_000_000, timezone.utc)
        session = {
            "id": stamp.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
            "started_ns": now_ns,
            "ended_ns": None,
        }
        payload["sessions"].append(session)
        # Keep a small extra metadata window for historical session review;
        # this catalog stores no captured CAN frames.
        cutoff = now_ns - 48 * 60 * 60 * 1_000_000_000
        payload["sessions"] = [
            item for item in payload["sessions"]
            if item.get("ended_ns") is None or int(item["ended_ns"]) >= cutoff
        ]
        self._write(payload)
        return dict(session)

    def end(self, *, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        payload = self._read()
        for session in reversed(payload["sessions"]):
            if session.get("ended_ns") is None:
                session["ended_ns"] = now_ns
                self._write(payload)
                return dict(session)
        return None

    def catalog(self):
        return self._read()

    def session_for(self, timestamp_ns, catalog=None):
        timestamp_ns = int(timestamp_ns)
        for session in reversed((catalog or self._read())["sessions"]):
            start = int(session["started_ns"])
            end = session.get("ended_ns")
            if timestamp_ns >= start and (end is None or timestamp_ns <= int(end)):
                return session["id"]
        return "unassigned"


@dataclass
class TransformPlan:
    lines: list[str]
    states: dict[tuple[str, str, str], dict]

    def line_protocol(self):
        return "\n".join(self.lines) + ("\n" if self.lines else "")

class TransformEngine:
    """Apply the initial TREVCAN live-analysis policy set."""

    STATE_NAME = "live-analysis-v1"

    def __init__(self, policy_path, session_file=None):
        self.policy_path = Path(policy_path)
        self.policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        self._validate_policy()
        self.sessions = SessionRegistry(session_file)
        self._committed = {}
        self._signal_sources = {}
        self._message_sources = {}
        for source_name, source in self.policy["sources"].items():
            buses = source.get("buses", [source.get("bus")])
            for bus in buses:
                message_key = (bus, source.get("dbc"), source.get("message"))
                if "signal" in source:
                    self._signal_sources[message_key + (source["signal"],)] = source_name
                else:
                    self._message_sources[message_key] = source_name

    def _validate_policy(self):
        if self.policy.get("version") != 1:
            raise ValueError("transform policy version must be 1")
        required = {"policies", "sources", "temperature_spike",
                    "current_positive_direction"}
        missing = sorted(required - self.policy.keys())
        if missing:
            raise ValueError(f"transform policy missing: {', '.join(missing)}")
        if self.policy["current_positive_direction"] not in {"charge", "discharge"}:
            raise ValueError("current_positive_direction must be charge or discharge")
        deadband = self.policy.get("speed_deadband_mph")
        if (not isinstance(deadband, (int, float)) or isinstance(deadband, bool) or
                not math.isfinite(deadband) or deadband < 0):
            raise ValueError("speed_deadband_mph must be a finite non-negative number")
        for name in ("fast", "medium", "slow", "event"):
            if name not in self.policy["policies"]:
                raise ValueError(f"transform policy missing policy {name}")

    def _new_state(self):
        return {
            "policy_version": self.policy["version"],
            "last_emit": {},
            "speed": {"last_t": None, "last_v": None, "distance_m": 0.0,
                      "window": []},
            "temperature": {"window": [], "last_warning_ns": None},
            "electrical": {
                "voltage": None, "voltage_t": None,
                "current": None, "current_t": None,
                "last_current": None, "last_current_t": None,
                "charge_added_ah": 0.0, "charge_removed_ah": 0.0,
                "last_power": None, "last_power_t": None,
                "energy_charged_wh": 0.0, "energy_used_wh": 0.0,
                "net_energy_wh": 0.0,
            },
            "imu": {"alignment_valid": False, "alignment_t": None},
        }

    def _state(self, work, session_id, car_id):
        key = (session_id, car_id, self.STATE_NAME)
        if key not in work:
            committed = self._committed.get(key)
            if not isinstance(committed, dict) or committed.get("policy_version") != 1:
                committed = self._new_state()
            work[key] = copy.deepcopy(committed)
        return work[key]

    def _period_ns(self, policy_name):
        return int(self.policy["policies"][policy_name]["publish_period_ms"] * 1_000_000)

    def _max_gap_ns(self, policy_name):
        return int(self.policy["policies"][policy_name]["max_gap_ms"] * 1_000_000)

    def _should_emit(self, state, name, timestamp_ns, policy_name):
        previous = state["last_emit"].get(name)
        period = self._period_ns(policy_name)
        if previous is not None and timestamp_ns - previous < period:
            return False
        state["last_emit"][name] = timestamp_ns
        return True

    def _derived(self, lines, state, *, car_id, session_id, timestamp_ns,
                 signal, value, unit, policy, source, force=False):
        if not force and not self._should_emit(state, signal, timestamp_ns, policy):
            return
        tags = (
            f"car={_escape_tag(car_id)},session={_escape_tag(session_id)},"
            f"policy={_escape_tag(policy)},signal={_escape_tag(signal)},"
            f"unit={_escape_tag(unit)},source={_escape_tag(source)}"
        )
        lines.append(
            f'telemetry_derived,{tags} value={float(value)},quality="good" {timestamp_ns}')

    def _event(self, lines, *, car_id, session_id, timestamp_ns, rule, signal,
               severity, message, fields):
        tags = (
            f"car={_escape_tag(car_id)},session={_escape_tag(session_id)},"
            f"severity={_escape_tag(severity)},rule={_escape_tag(rule)},"
            f"signal={_escape_tag(signal)}"
        )
        values = [f'message="{_escape_field(message)}"']
        values.extend(f"{name}={float(value)}" for name, value in fields.items())
        lines.append(f"telemetry_event,{tags} {','.join(values)} {timestamp_ns}")

    @staticmethod
    def _slope(window):
        if len(window) < 2:
            return None
        origin = window[0][0]
        points = [((timestamp - origin) / 1_000_000_000, value)
                  for timestamp, value in window]
        mean_t = sum(item[0] for item in points) / len(points)
        mean_v = sum(item[1] for item in points) / len(points)
        denominator = sum((timestamp - mean_t) ** 2 for timestamp, _ in points)
        if denominator <= 0:
            return None
        return sum((timestamp - mean_t) * (value - mean_v)
                   for timestamp, value in points) / denominator

    def _handle_speed(self, lines, state, frame, value, session_id):
        timestamp = frame["timestamp_ns"]
        # Preserve the decoded source signal, but prevent a stationary one-LSB
        # speed flicker from accumulating false distance or acceleration.
        filtered_mph = (0.0 if abs(value) <= self.policy["speed_deadband_mph"]
                        else value)
        speed_mps = filtered_mph * 0.44704
        speed = state["speed"]
        previous_t, previous_v = speed["last_t"], speed["last_v"]
        if previous_t is not None and timestamp > previous_t:
            elapsed = timestamp - previous_t
            if elapsed <= self._max_gap_ns("fast"):
                speed["distance_m"] += (previous_v + speed_mps) * 0.5 * (
                    elapsed / 1_000_000_000)
        if previous_t is None or timestamp >= previous_t:
            speed["last_t"], speed["last_v"] = timestamp, speed_mps
        window_ns = int(self.policy["derivatives"]["speed_window_ms"] * 1_000_000)
        speed["window"] = [sample for sample in speed["window"]
                           if sample[0] >= timestamp - window_ns and sample[0] < timestamp]
        speed["window"].append([timestamp, speed_mps])
        acceleration = self._slope(speed["window"])
        common = {"car_id": frame["car_id"], "session_id": session_id,
                  "timestamp_ns": timestamp}
        self._derived(lines, state, **common, signal="Vehicle_Speed_mps",
                      value=speed_mps, unit="m/s", policy="fast",
                      source="VCU_Speed_MPH")
        self._derived(lines, state, **common, signal="Distance_m",
                      value=speed["distance_m"], unit="m", policy="medium",
                      source="VCU_Speed_MPH")
        if acceleration is not None:
            self._derived(lines, state, **common,
                          signal="Vehicle_Accel_From_Speed_mps2",
                          value=acceleration, unit="m/s^2", policy="fast",
                          source="VCU_Speed_MPH")

    def _handle_temperature(self, lines, state, frame, value, session_id):
        timestamp = frame["timestamp_ns"]
        temperature = state["temperature"]
        derivative_ns = int(
            self.policy["derivatives"]["temperature_window_ms"] * 1_000_000)
        spike = self.policy["temperature_spike"]
        spike_ns = int(spike["window_ms"] * 1_000_000)
        keep_ns = max(derivative_ns, spike_ns)
        temperature["window"] = [
            sample for sample in temperature["window"]
            if sample[0] >= timestamp - keep_ns and sample[0] < timestamp
        ]
        recent = [sample for sample in temperature["window"]
                  if sample[0] >= timestamp - spike_ns]
        if spike.get("enabled", False) and recent:
            previous_value = max(recent, key=lambda sample: abs(value - sample[1]))[1]
            delta = value - previous_value
            cooldown_ns = int(spike["cooldown_ms"] * 1_000_000)
            last_warning = temperature.get("last_warning_ns")
            if (abs(delta) >= float(spike["delta_c"]) and
                    (last_warning is None or timestamp - last_warning >= cooldown_ns)):
                self._event(
                    lines, car_id=frame["car_id"], session_id=session_id,
                    timestamp_ns=timestamp, rule="temperature_spike",
                    signal="HVC_Acc_Temp_Max_C", severity="warning",
                    message=(f"Accumulator maximum temperature changed {delta:+.1f} degC "
                             f"within {spike['window_ms'] / 1000:g} s"),
                    fields={"delta_c": delta, "threshold_c": spike["delta_c"],
                            "value_c": value},
                )
                temperature["last_warning_ns"] = timestamp
        temperature["window"].append([timestamp, value])
        slope_window = [sample for sample in temperature["window"]
                        if sample[0] >= timestamp - derivative_ns]
        slope = self._slope(slope_window)
        if slope is not None:
            self._derived(
                lines, state, car_id=frame["car_id"], session_id=session_id,
                timestamp_ns=timestamp, signal="Accumulator_Temp_Rise_C_per_s",
                value=slope, unit="degC/s", policy="slow",
                source="HVC_Acc_Temp_Max_C")

    @staticmethod
    def _integrate_split(previous, current, elapsed_seconds):
        if previous >= 0 and current >= 0:
            positive = (previous + current) * 0.5 * elapsed_seconds
            negative = 0.0
        elif previous <= 0 and current <= 0:
            positive = 0.0
            negative = (-previous - current) * 0.5 * elapsed_seconds
        else:
            # Assume a straight line between samples and split the two
            # triangles at its zero crossing instead of counting both over the
            # full interval.
            crossing = abs(previous) / (abs(previous) + abs(current))
            if previous > 0:
                positive = previous * crossing * elapsed_seconds * 0.5
                negative = -current * (1 - crossing) * elapsed_seconds * 0.5
            else:
                negative = -previous * crossing * elapsed_seconds * 0.5
                positive = current * (1 - crossing) * elapsed_seconds * 0.5
        signed = (previous + current) * 0.5 * elapsed_seconds
        return positive, negative, signed

    def _handle_current(self, state, timestamp, value):
        electrical = state["electrical"]
        previous_t, previous = electrical["last_current_t"], electrical["last_current"]
        if previous_t is not None and timestamp > previous_t:
            elapsed_ns = timestamp - previous_t
            if elapsed_ns <= self._max_gap_ns("medium"):
                positive, negative, _ = self._integrate_split(
                    previous, value, elapsed_ns / 1_000_000_000)
                if self.policy["current_positive_direction"] == "charge":
                    electrical["charge_added_ah"] += positive / 3600
                    electrical["charge_removed_ah"] += negative / 3600
                else:
                    electrical["charge_added_ah"] += negative / 3600
                    electrical["charge_removed_ah"] += positive / 3600
        if previous_t is None or timestamp >= previous_t:
            electrical["last_current_t"], electrical["last_current"] = timestamp, value
            electrical["current_t"], electrical["current"] = timestamp, value

    def _handle_electrical(self, lines, state, frame, source_name, value, session_id):
        timestamp = frame["timestamp_ns"]
        electrical = state["electrical"]
        if source_name == "pack_voltage":
            electrical["voltage_t"], electrical["voltage"] = timestamp, value
        else:
            self._handle_current(state, timestamp, value)
        voltage_t, current_t = electrical["voltage_t"], electrical["current_t"]
        if voltage_t is None or current_t is None:
            return
        max_age_ns = int(self.policy["electrical_pair_max_age_ms"] * 1_000_000)
        if abs(voltage_t - current_t) > max_age_ns:
            return
        timestamp = max(voltage_t, current_t)
        power = electrical["voltage"] * electrical["current"]
        previous_t, previous = electrical["last_power_t"], electrical["last_power"]
        if previous_t is not None and timestamp > previous_t:
            elapsed_ns = timestamp - previous_t
            if elapsed_ns <= self._max_gap_ns("medium"):
                positive, negative, signed = self._integrate_split(
                    previous, power, elapsed_ns / 1_000_000_000)
                if self.policy["current_positive_direction"] == "charge":
                    electrical["energy_charged_wh"] += positive / 3600
                    electrical["energy_used_wh"] += negative / 3600
                    electrical["net_energy_wh"] += signed / 3600
                else:
                    electrical["energy_charged_wh"] += negative / 3600
                    electrical["energy_used_wh"] += positive / 3600
                    electrical["net_energy_wh"] -= signed / 3600
        if previous_t is None or timestamp >= previous_t:
            electrical["last_power_t"], electrical["last_power"] = timestamp, power
        common = {"car_id": frame["car_id"], "session_id": session_id,
                  "timestamp_ns": timestamp, "policy": "medium"}
        values = [
            ("Pack_Power_W", power, "W", "HVC_Batt_Voltage_V+HVC_Pack_Current_A"),
            ("Charge_Added_Ah", electrical["charge_added_ah"], "Ah", "HVC_Pack_Current_A"),
            ("Charge_Removed_Ah", electrical["charge_removed_ah"], "Ah", "HVC_Pack_Current_A"),
            ("Energy_Charged_Wh", electrical["energy_charged_wh"], "Wh", "Pack_Power_W"),
            ("Energy_Used_Wh", electrical["energy_used_wh"], "Wh", "Pack_Power_W"),
            ("Net_Energy_Change_Wh", electrical["net_energy_wh"], "Wh", "Pack_Power_W"),
        ]
        for signal, output, unit, source in values:
            self._derived(lines, state, **common, signal=signal, value=output,
                          unit=unit, source=source)

    def _handle_alignment(self, lines, state, frame, signals, session_id):
        required = ("AlignStatus", "AlignXYError", "AlignZError", "AlignError")
        if not all(name in signals and _numeric(signals[name]) is not None for name in required):
            return
        valid = (int(_numeric(signals["AlignStatus"])) in
                 set(self.policy["imu_alignment"]["accepted_status_values"]) and
                 all(int(_numeric(signals[name])) == 0 for name in required[1:]))
        imu = state["imu"]
        changed = bool(valid) != bool(imu["alignment_valid"])
        imu["alignment_valid"], imu["alignment_t"] = bool(valid), frame["timestamp_ns"]
        should_emit = self._should_emit(
            state, "IMU_Alignment_Valid", frame["timestamp_ns"], "event")
        if changed or should_emit:
            self._derived(
                lines, state, car_id=frame["car_id"], session_id=session_id,
                timestamp_ns=frame["timestamp_ns"], signal="IMU_Alignment_Valid",
                value=1 if valid else 0, unit="bool", policy="event",
                source="AlignStatus", force=True)

    def _handle_imu(self, lines, state, frame, signals, session_id):
        names = ("ImuValid", "AccelerationX", "AccelerationY")
        if not all(name in signals and _numeric(signals[name]) is not None for name in names):
            return
        imu = state["imu"]
        freshness_ns = int(self.policy["imu_alignment"]["max_age_ms"] * 1_000_000)
        aligned = (imu["alignment_valid"] and imu["alignment_t"] is not None and
                   0 <= frame["timestamp_ns"] - imu["alignment_t"] <= freshness_ns)
        if int(_numeric(signals["ImuValid"])) != 1 or not aligned:
            return
        timestamp = frame["timestamp_ns"]
        if not self._should_emit(state, "vehicle_dynamics", timestamp, "fast"):
            return
        longitudinal = _numeric(signals["AccelerationX"]) / 9.80665
        lateral = _numeric(signals["AccelerationY"]) / 9.80665
        combined = math.hypot(longitudinal, lateral)
        tags = (f"car={_escape_tag(frame['car_id'])},session={_escape_tag(session_id)},"
                "source=CAN9_IMU,unit=g")
        lines.append(
            f"vehicle_dynamics,{tags} longitudinal_g={longitudinal},"
            f"lateral_g={lateral},combined_g={combined},alignment_valid=true {timestamp}")
        for signal, value in (("Longitudinal_Accel_g", longitudinal),
                              ("Lateral_Accel_g", lateral),
                              ("Combined_Accel_g", combined)):
            self._derived(
                lines, state, car_id=frame["car_id"], session_id=session_id,
                timestamp_ns=timestamp, signal=signal, value=value, unit="g",
                policy="fast", source="CAN9_ImuData", force=True)

    def prepare(self, decoded_frames):
        """Return line protocol and proposed state for decoded ``(frame, result)`` pairs."""
        lines = []
        work = {}
        session_catalog = self.sessions.catalog()
        ordered = sorted(decoded_frames, key=lambda item: (
            item[0]["timestamp_ns"], item[0].get("seq", 0)))
        for frame, decoded in ordered:
            if decoded is None:
                continue
            dbc_file, message_name, signals = decoded
            session_id = self.sessions.session_for(
                frame["timestamp_ns"], session_catalog)
            state = self._state(work, session_id, frame["car_id"])
            message_source = self._message_sources.get(
                (frame.get("bus", "unknown"), dbc_file, message_name))
            if message_source == "imu_alignment":
                self._handle_alignment(lines, state, frame, signals, session_id)
            if message_source == "imu_data":
                self._handle_imu(lines, state, frame, signals, session_id)
            for signal_name, raw_value in signals.items():
                value = _numeric(raw_value)
                if value is None:
                    continue
                source_name = self._signal_sources.get((
                    frame.get("bus", "unknown"), dbc_file, message_name, signal_name))
                if source_name == "vehicle_speed":
                    self._handle_speed(lines, state, frame, value, session_id)
                elif source_name == "pack_voltage":
                    self._handle_electrical(
                        lines, state, frame, "pack_voltage", value, session_id)
                elif source_name == "pack_current":
                    self._handle_electrical(
                        lines, state, frame, "pack_current", value, session_id)
                elif source_name == "accumulator_temp_max":
                    self._handle_temperature(lines, state, frame, value, session_id)
        return TransformPlan(lines, work)

    def commit(self, plan):
        self._committed.update(copy.deepcopy(plan.states))

