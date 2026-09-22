"""Wire format shared by the car agent and laptop receiver."""

import json


def validate_frame(frame):
    if not isinstance(frame, dict):
        raise ValueError("frame must be a JSON object")
    if frame.get("type") != "frame":
        raise ValueError("expected frame")
    car_id = frame.get("car_id")
    if not isinstance(car_id, str) or not car_id or len(car_id) > 128:
        raise ValueError("invalid car_id")
    for key in ("seq", "timestamp_ns", "can_id", "dlc"):
        if type(frame.get(key)) is not int:
            raise ValueError(f"invalid {key}")
    if frame["seq"] < 1 or frame["timestamp_ns"] < 1:
        raise ValueError("invalid sequence or timestamp")
    if type(frame.get("is_extended")) is not bool or type(frame.get("is_remote")) is not bool:
        raise ValueError("invalid CAN flags")
    if type(frame.get("is_fd")) is not bool:
        raise ValueError("invalid CAN FD flag")
    if type(frame.get("is_error")) is not bool:
        raise ValueError("invalid CAN error flag")
    if not 0 <= frame["can_id"] <= (0x1FFFFFFF if frame["is_extended"] else 0x7FF):
        raise ValueError("invalid CAN ID")
    max_len = 64 if frame["is_fd"] else 8
    if not 0 <= frame["dlc"] <= max_len:
        raise ValueError("invalid DLC")
    data = frame.get("data")
    if not isinstance(data, str) or len(data) > max_len * 2:
        raise ValueError("invalid data")
    try:
        payload = bytes.fromhex(data)
    except ValueError as exc:
        raise ValueError("invalid hex data") from exc
    if len(payload) > max_len or (not frame["is_remote"] and len(payload) != frame["dlc"]):
        raise ValueError("data length does not match DLC")
    return payload


def wire_line(message):
    return (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
