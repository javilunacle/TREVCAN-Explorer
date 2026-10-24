"""Wire format shared by the car agent and telemetry Pi receiver."""

import json
import math


# Shared validation and JSON framing for car-to-Pi traffic. Validation returns
# the raw payload as bytes; it never DBC-decodes or changes the supplied record.


def validate_frame(frame):
    # Check the envelope and routing identity before interpreting CAN fields.
    if not isinstance(frame, dict):
        raise ValueError("frame must be a JSON object")
    if frame.get("type") != "frame":
        raise ValueError("expected frame")
    # Identify the sender run so sequence numbers can restart after that process
    # restarts without the receiver treating new samples as old duplicates.
    stream_id = frame.get("stream_id")
    if not isinstance(stream_id, str) or not stream_id or len(stream_id) > 128:
        raise ValueError("invalid stream_id")
    bus = frame.get("bus", "unknown")
    if (not isinstance(bus, str) or not bus or len(bus) > 64 or
            any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in bus)):
        raise ValueError("invalid bus")
    # Exact integer checks also reject booleans, which Python otherwise treats
    # as integers. These fields identify and order samples on the wire.
    for key in ("seq", "timestamp_ns", "can_id", "dlc"):
        if type(frame.get(key)) is not int:
            raise ValueError(f"invalid {key}")
    if frame["seq"] < 1 or frame["timestamp_ns"] < 1:
        raise ValueError("invalid sequence or timestamp")
    # Preserve CAN frame type explicitly: standard/extended, remote, FD, and
    # error frames have different ID or payload rules below.
    if type(frame.get("is_extended")) is not bool or type(frame.get("is_remote")) is not bool:
        raise ValueError("invalid CAN flags")
    if type(frame.get("is_fd")) is not bool:
        raise ValueError("invalid CAN FD flag")
    if type(frame.get("is_error")) is not bool:
        raise ValueError("invalid CAN error flag")
    # Standard CAN IDs are 11 bits; extended CAN IDs are 29 bits.
    if not 0 <= frame["can_id"] <= (0x1FFFFFFF if frame["is_extended"] else 0x7FF):
        raise ValueError("invalid CAN ID")
    # Classical CAN carries up to 8 data bytes; CAN FD carries up to 64.
    max_len = 64 if frame["is_fd"] else 8
    if not 0 <= frame["dlc"] <= max_len:
        raise ValueError("invalid DLC")
    data = frame.get("data")
    if not isinstance(data, str) or len(data) > max_len * 2:
        raise ValueError("invalid data")
    try:
        # Hex text keeps the raw bytes JSON-compatible across the TCP connection.
        payload = bytes.fromhex(data)
    except ValueError as exc:
        raise ValueError("invalid hex data") from exc
    # A remote frame can request a DLC without carrying that many payload bytes.
    if len(payload) > max_len or (not frame["is_remote"] and len(payload) != frame["dlc"]):
        raise ValueError("data length does not match DLC")
    # Raw-only senders remain valid. Paired records must also be safe for the
    # existing JSON TCP sender; non-finite dashboard samples arrive as null.
    if "decoded" in frame:
        decoded = frame["decoded"]
        if not isinstance(decoded, dict):
            raise ValueError("invalid decoded signals")
        for name, value in decoded.items():
            if (not isinstance(name, str) or
                    (value is not None and not isinstance(value, (int, float, bool, str))) or
                    (isinstance(value, float) and not math.isfinite(value))):
                raise ValueError("invalid decoded signal value")
    # Source names and choice labels come from the same CANManager decode.
    # Standalone raw-only capture and replay may omit these fields.
    for name in ("dbc", "message"):
        if name in frame and not isinstance(frame[name], str):
            raise ValueError(f"invalid {name}")
    labels = frame.get("labels", {})
    if not isinstance(labels, dict) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in labels.items()):
        raise ValueError("invalid decoded choice labels")
    # Callers retain the frame metadata and decoded dictionary alongside these bytes.
    return payload


def wire_line(message):
    # TCP is a byte stream, so a trailing newline marks each complete JSON
    # message for reader.readline(). Reject NaN/Infinity instead of emitting invalid JSON.
    return (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")



def point_line(point):
    """Format one calculated sample for either InfluxDB or Grafana Live."""
    # The engine returns ordinary data; formatting and network delivery belong
    # to the output workers. Both workers consume the same calculated result.
    def tag(value):
        return (str(value).replace("\r", " ").replace("\n", " ")
                .replace("\\", "\\\\").replace(" ", "\\ ")
                .replace(",", "\\,").replace("=", "\\="))

    def field(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int):
            return f"{value}i"
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite calculated value")
            return str(value)
        return '"' + (str(value).replace("\\", "\\\\").replace('"', '\\"')
                      .replace("\r", " ").replace("\n", " ")) + '"'

    tags = "".join(f",{tag(name)}={tag(value)}"
                   for name, value in point["tags"].items())
    fields = ",".join(f"{tag(name)}={field(value)}"
                      for name, value in point["fields"].items())
    return f"{point['measurement']}{tags} {fields} {point['timestamp_ns']}"
