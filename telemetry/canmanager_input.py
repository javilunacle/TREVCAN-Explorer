"""Receive CANManager's paired raw/decoded records through a local Linux socket."""

import asyncio
import json
import socket

from .common import validate_frame


# Match the dashboard's default. The leading NUL below selects Linux's abstract
# namespace, so stopping telemetry releases the name without socket-file cleanup.
DEFAULT_SOCKET_NAME = "trevcan-telemetry"


# This is telemetry's local input adapter. CANManager has already decoded each
# frame in the dashboard process; this module validates and queues that paired
# record for car.py's capture_writer, without decoding the payload again.


class CANManagerInput(asyncio.DatagramProtocol):
    def __init__(self, queue):
        # This queue belongs to the telemetry process and is consumed by its
        # batching task. The local socket is what crosses the process boundary.
        self.queue = queue
        self.dropped = 0
        self.invalid = 0

    def datagram_received(self, data, address):
        try:
            # asyncio calls this once per complete datagram; raw and decoded
            # fields arrive together, rather than as separate messages to match.
            record = json.loads(data)
            # Reuse raw-frame validation before enqueueing. These temporary
            # identity fields are not stored; CarBuffer assigns the real ones.
            payload = validate_frame({**record, "type": "frame",
                                      "stream_id": "canmanager", "seq": 1})
            decoded = record["decoded"]
            if not isinstance(decoded, dict):
                raise ValueError("decoded signals must be an object")

            # Keep this decode's source names and enum labels beside its values.
            # CarBuffer adds the stream ID and sequence after this local handoff.
            metadata = {name: record.get(name, "") for name in ("dbc", "message")}
            metadata["labels"] = record.get("labels", {})
            self.queue.put_nowait((record["bus"], record["can_id"], payload,
                                   record["is_extended"], record["is_remote"],
                                   record["is_fd"], record["is_error"], record["dlc"],
                                   record["timestamp_ns"], decoded, metadata))
        except asyncio.QueueFull:
            # Match the bounded, fresh-data pipeline: never stall CANManager.
            self.dropped += 1
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            # Ignore a malformed local record and keep receiving later frames.
            self.invalid += 1


async def capture_canmanager_forever(queue, socket_name):
    loop = asyncio.get_running_loop()
    # Bind one receiver to the same abstract socket name used by TelemetryData.
    # The name is local to Linux; it is separate from the off-car TCP address.
    transport, receiver = await loop.create_datagram_endpoint(
        lambda: CANManagerInput(queue), local_addr="\0" + socket_name,
        family=socket.AF_UNIX)
    print(f"[car] receiving CANManager frames on @{socket_name}", flush=True)
    try:
        # Datagram callbacks feed the same queue used by capture_writer.
        # Stay alive until the enclosing car.py task group cancels this input.
        await asyncio.Future()
    finally:
        # Closing the transport releases the abstract name for the next run.
        transport.close()
        if receiver.dropped or receiver.invalid:
            print(f"[canmanager-input] dropped={receiver.dropped} invalid={receiver.invalid}",
                  flush=True)
