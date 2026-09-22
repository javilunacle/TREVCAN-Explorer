"""Build a synthetic raw-CAN replay file from the enabled dashboard DBCs.

This runs off-car. The replay file contains raw frame IDs and bytes only; the
car-side replay path does not load a DBC and never transmits onto SocketCAN.
"""

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path


DBC_DIRECTORY = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files"
DEFAULT_DBC_FILES = (
    "BMS-Firmware-RTOS-Complete.dbc",
    "hvc.dbc",
    "BMS-Inverter-Only.dbc",
    "master.dbc",
    "Baby_MOBO.dbc",
)
RAW_KEYS = ("can_id", "is_extended", "is_remote", "is_fd", "is_error", "dlc", "data")
CONTROL_NAME = re.compile(r"Command|Request|_ACK|Reset|^SET_|Passthrough", re.IGNORECASE)


def selected_messages(paths):
    """Use the same first-matching-ID priority that the receiver uses."""
    import cantools

    seen = set()
    selected = []
    for path in paths:
        database = cantools.database.load_file(path, strict=False)
        for message in database.messages:
            key = (message.frame_id, bool(message.is_extended_frame))
            if key in seen:
                continue
            seen.add(key)
            if not message.signals or CONTROL_NAME.search(message.name):
                continue
            selected.append((Path(path).name, message))
    return selected


def _signal_value(signal, cycle, index):
    """Pick a deterministic, illustrative value within the DBC's raw range."""
    name = signal.name.lower()
    unit = (signal.unit or "").lower()
    wave = math.sin(cycle * 1.2 + index * 0.17)
    if signal.is_multiplexer:
        value = min(signal.multiplexer_ids or [0])
    elif "heartbeat" in name:
        value = cycle + 1
    elif "cellvoltage" in name or "cell_voltage" in name:
        value = 3700 + 40 * wave if unit == "mv" else 3.7 + 0.04 * wave
    elif "temp" in name or "therm" in name:
        value = 30 + 4 * wave
    elif "voltage" in name or "v_sense" in name or "vsense" in name:
        if unit == "mv":
            value = (380000 if any(word in name for word in ("batt", "inv", "stack", "bus"))
                     else 3300) * (1 + 0.01 * wave)
        elif unit == "v":
            value = (380 if signal.maximum is None or signal.maximum > 100 else 12.5) * (1 + 0.01 * wave)
        else:
            value = 12 + wave
    elif "current" in name:
        value = (12000 if unit == "ma" else 12) * (1 + 0.1 * wave)
    elif "speed" in name or unit == "rpm":
        value = 1200 + 150 * wave
    elif "torque" in name:
        value = 40 + 5 * wave
    elif "soc" in name or "state_of_charge" in name:
        value = 65 + 2 * wave
    elif "pressure" in name or unit == "psi":
        value = 45 + 3 * wave
    elif unit == "%" or "apps" in name and "value" in name:
        value = 25 + 5 * wave
    elif "shock" in name and unit == "mm":
        value = 30 + 3 * wave
    elif "state" in name or "valid" in name or "ready" in name:
        value = 1
    else:
        value = 0

    # Clamp to both the representable raw range and declared engineering limits.
    raw_min = -(1 << (signal.length - 1)) if signal.is_signed else 0
    raw_max = (1 << (signal.length - 1)) - 1 if signal.is_signed else (1 << signal.length) - 1
    if signal.scale == 0:
        raise ValueError(f"zero-scale signal: {signal.name}")
    raw = round((value - signal.offset) / signal.scale)
    raw = min(max(raw, raw_min), raw_max)
    if signal.minimum is not None:
        raw = max(raw, math.ceil((signal.minimum - signal.offset) / signal.scale))
    if signal.maximum is not None:
        raw = min(raw, math.floor((signal.maximum - signal.offset) / signal.scale))
    return raw * signal.scale + signal.offset


def generate_frames(paths, cycles=2):
    """Return a repeatable set of raw frames; each selected message appears per cycle."""
    if cycles < 1:
        raise ValueError("cycles must be positive")
    selected = selected_messages(paths)
    if not selected:
        raise ValueError("no telemetry messages found in the selected DBCs")
    frames = []
    for cycle in range(cycles):
        for index, (filename, message) in enumerate(selected):
            values = {signal.name: _signal_value(signal, cycle, index + offset)
                      for offset, signal in enumerate(message.signals)}
            data = message.encode(values, strict=False)
            frames.append({"can_id": message.frame_id,
                           "is_extended": bool(message.is_extended_frame),
                           "is_remote": False, "is_fd": len(data) > 8,
                           "is_error": False, "dlc": len(data),
                           "data": data.hex().upper(),
                           "source_dbc": filename, "message": message.name})
    return frames


def main():
    parser = argparse.ArgumentParser(description="Generate raw telemetry frames for an off-car demo")
    parser.add_argument("--output", required=True, help="JSONL replay file to create")
    parser.add_argument("--dbc", action="append", help="DBC priority order; repeat for more files")
    parser.add_argument("--cycles", type=int, default=2,
                        help="number of slightly different samples per telemetry message")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("cycles must be positive")
    paths = args.dbc or [DBC_DIRECTORY / name for name in DEFAULT_DBC_FILES]
    frames = generate_frames(paths, args.cycles)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for frame in frames:
            stream.write(json.dumps({key: frame[key] for key in RAW_KEYS},
                                    separators=(",", ":")) + "\n")
    counts = Counter(frame["source_dbc"] for frame in frames)
    print(json.dumps({"output": str(output), "frames": len(frames),
                      "messages_per_cycle": len(frames) // args.cycles,
                      "cycles": args.cycles, "by_dbc": counts}, indent=2))


if __name__ == "__main__":
    main()
