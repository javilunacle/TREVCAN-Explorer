"""Pi multi-bus capture with small RAM batches and fresh-data reconnects."""

import argparse
import asyncio
import json
import math
import os
import sys
import time

from .common import validate_frame, wire_line
from .buffers import CarBuffer
from .canmanager_input import DEFAULT_SOCKET_NAME, capture_canmanager_forever


# Data flow through this process:
#   Input -> bounded capture queue -> capture_writer -> CarBuffer -> TCP sender.
# The installed service uses CANManager input: the dashboard receives SocketCAN
# frames, decodes them, and sends raw/decoded pairs through the local socket.
# Direct --interface mode instead reads raw SocketCAN frames in capture_forever.
# Both input paths feed the same queue, buffer, and sender below.


async def send_forever(buffer, host, port, token=None, stop=None, window_size=64):
    """Send small batches; a RAM sender discards disconnected data."""
    stop = stop or asyncio.Event()
    retry_delay = 1
    last_error_print = float("-inf")
    while not stop.is_set():
        try:
            # This TCP connection goes to the off-car Pi. Capture continues in
            # its own task while the sender connects, waits for ACKs, or retries.
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
            buffer.set_connected(True)
            announced_connected = False
            try:
                while not stop.is_set():
                    # Read up to window_size pending records without removing
                    # them. Records leave CarBuffer only after an ACK or a drop.
                    batch = buffer.pending(window_size)
                    if not batch:
                        try:
                            await asyncio.wait_for(stop.wait(), 0.02)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    # Send each complete frame dictionary, including decoded
                    # signals when the source is CANManager, as a JSON-line batch.
                    message = {"type": "batch", "frames": batch}
                    if token:
                        message["token"] = token
                    writer.write(wire_line(message))
                    await asyncio.wait_for(writer.drain(), 2)
                    # The receiver ACKs the last sequence in this batch after
                    # accepting it into RAM; this does not confirm disk storage.
                    raw = await asyncio.wait_for(reader.readline(), 2)
                    if not raw:
                        raise ConnectionError("receiver disconnected")
                    reply = json.loads(raw)
                    if reply.get("type") == "error":
                        raise ValueError(f"receiver rejected batch: {reply.get('error')}")
                    if reply.get("type") != "ack" or reply.get("seq") != batch[-1]["seq"]:
                        raise ValueError("unexpected cumulative ACK")
                    buffer.acknowledge_through(reply["seq"])
                    if not announced_connected:
                        print(f"[sender] connected to {host}:{port}", flush=True)
                        announced_connected = True
                    retry_delay = 1
                    last_error_print = float("-inf")
            finally:
                # Marking the link disconnected discards pending RAM records.
                # Reconnection starts with fresh capture rather than old backlog.
                buffer.set_connected(False)
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except (OSError, asyncio.TimeoutError):
                    pass
        except (OSError, asyncio.TimeoutError, ConnectionError,
                json.JSONDecodeError, ValueError) as exc:
            buffer.set_connected(False)
            now = asyncio.get_running_loop().time()
            if now - last_error_print >= 30:
                print(f"[sender] disconnected: {exc}; retrying in {retry_delay}s", flush=True)
                last_error_print = now
            try:
                await asyncio.wait_for(stop.wait(), retry_delay)
            except asyncio.TimeoutError:
                pass


def load_simulation_frames(path):
    """Validate a raw-frame replay file before anything is placed in the buffer."""
    frames = []
    with open(path, encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
                # Supply temporary identity fields for validation. The actual
                # stream identity and sequences are assigned later by CarBuffer.
                candidate = {**frame, "type": "frame", "stream_id": "simulation",
                             "seq": line_number, "timestamp_ns": 1}
                data = validate_frame(candidate)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid replay frame at line {line_number}: {exc}") from exc
            frames.append((frame.get("bus", "simulation"), frame["can_id"], data,
                           frame["is_extended"],
                           frame["is_remote"], frame["is_fd"], frame["is_error"],
                           frame["dlc"]))
    if not frames:
        raise ValueError("replay file contains no frames")
    return frames


async def capture_forever(queue, *, interface=None, simulate_rate=None,
                          simulate_file=None, replay_rate=50):
    # These inputs are selected for standalone capture or testing. The installed
    # CANManager input uses capture_canmanager_forever instead (see run below).
    if simulate_file:
        # Replay raw frames from a file with a fresh timestamp for each sample.
        frames = load_simulation_frames(simulate_file)
        print(f"[simulation] replaying {len(frames)} raw frames at {replay_rate:g} frames/s; "
              "SocketCAN is not used", flush=True)
        index = 0
        while True:
            await queue.put((*frames[index], time.time_ns()))
            index = (index + 1) % len(frames)
            await asyncio.sleep(1 / replay_rate)
    elif simulate_rate:
        # Generate raw bytes at the requested rate without opening a CAN interface.
        index = 0
        while True:
            # master.dbc, ID 160: INV_Temps_1, four little-endian signed 16-bit temperatures.
            temp_tenths = round((25 + 8 * math.sin(index / 20)) * 10)
            data = b"".join(int(temp_tenths + offset).to_bytes(2, "little", signed=True)
                            for offset in (0, 20, 40, 60))
            await queue.put(("simulation", 160, data, False, False, False, False,
                             len(data), time.time_ns()))
            index += 1
            await asyncio.sleep(1 / simulate_rate)
    else:
        # --interface selects direct SocketCAN capture. Each interface gets its
        # own capture task, so --interface can0 --interface can1 reads both buses.
        try:
            import can
        except ImportError as exc:
            raise RuntimeError("python-can is required for SocketCAN capture") from exc
        # Open a receive socket for the selected, already configured interface.
        # Frames here are still raw: no DBC decoding happens in this function.
        with can.Bus(interface="socketcan", channel=interface) as bus:
            while True:
                # recv waits up to 0.5 seconds for one CAN frame, returning None
                # on timeout. A worker thread handles that blocking wait so the
                # asyncio loop can keep batching and sending at the same time.
                msg = await asyncio.to_thread(bus.recv, 0.5)
                if msg is not None:
                    # Keep the source bus, ID, payload bytes, flags, DLC, and
                    # local receive time together as one capture record. There
                    # is no sequence yet; capture_writer passes it to CarBuffer.
                    # Backpressure is visible here; a full queue may cause kernel CAN loss.
                    await queue.put((interface, msg.arbitration_id, bytes(msg.data), msg.is_extended_id,
                                     msg.is_remote_frame, msg.is_fd, msg.is_error_frame,
                                     msg.dlc, time.time_ns()))


async def capture_writer(buffer, queue, batch_size=256, flush_ms=20, status_interval=5):
    last_status = asyncio.get_running_loop().time()
    while True:
        # Wait for the first record, then collect more until the batch reaches
        # batch_size or flush_ms elapses. Input can be raw-only or raw/decoded.
        records = [await queue.get()]
        deadline = asyncio.get_running_loop().time() + flush_ms / 1000
        while len(records) < batch_size:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                records.append(await asyncio.wait_for(queue.get(), remaining))
            except asyncio.TimeoutError:
                break
        # CarBuffer builds outgoing frame dictionaries and assigns sequences.
        # It retains records only while TCP is connected, within its RAM limits.
        buffer.append_many(records)
        for _ in records:
            # Mark queue processing complete; the remote ACK happens separately.
            queue.task_done()
        now = asyncio.get_running_loop().time()
        if now - last_status >= status_interval:
            print(f"[car-buffer] {buffer.status()}", flush=True)
            last_status = now


async def run(args):
    # This process owns the streaming buffer and its sequence numbers. The
    # capture queue is the short handoff between input and the batching task.
    buffer = CarBuffer(args.stream_id)
    print(f"[car] {buffer.status()}", flush=True)
    queue = asyncio.Queue(maxsize=args.queue_size)
    try:
        # Run input, batching, and TCP sending concurrently on one asyncio loop.
        # Cancelling this group also stops its input and sender tasks together.
        async with asyncio.TaskGroup() as group:
            if args.canmanager_socket is not None:
                # Live car input now comes from the dashboard's single decoder.
                # Batching, sequencing, and the off-car TCP sender stay below.
                # Its local socket receiver puts raw/decoded pairs into queue.
                group.create_task(capture_canmanager_forever(queue, args.canmanager_socket))
            else:
                # Preserve standalone SocketCAN capture and synthetic test inputs.
                # Hardware mode creates one reader per requested interface;
                # simulation mode creates a single reader with interface=None.
                interfaces = args.interface or [None]
                for interface in interfaces:
                    group.create_task(capture_forever(queue, interface=interface,
                                                      simulate_rate=args.simulate_rate,
                                                      simulate_file=args.simulate_file,
                                                      replay_rate=args.replay_rate))
            # Both source choices join the same remaining pipeline here.
            group.create_task(capture_writer(buffer, queue, args.batch_size,
                                           args.flush_ms, args.status_interval))
            group.create_task(send_forever(buffer, args.host, args.port, args.token,
                                           window_size=args.window_size))
    finally:
        buffer.close()


def main():
    parser = argparse.ArgumentParser(description="Pi raw-CAN capture and RAM-batched sender")
    parser.add_argument("--host", default="127.0.0.1", help="telemetry receiver host")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("TELEMETRY_TOKEN"),
                        help="shared demo token (or TELEMETRY_TOKEN environment variable)")
    parser.add_argument("--stream-id",
                        help="optional stream prefix; each sender run adds a unique suffix")
    # Select one input mode. Repeat --interface within hardware mode to capture
    # multiple buses; CANManager mode already carries both buses through one socket.
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--canmanager-socket", nargs="?",
                        const=os.getenv("TREVCAN_TELEMETRY_SOCKET", DEFAULT_SOCKET_NAME),
                        metavar="NAME", help="receive raw/decoded pairs from CANManager (Linux)")
    source.add_argument("--interface", action="append",
                        help="SocketCAN interface; repeat for can0 and can1")
    source.add_argument("--simulate-rate", type=float, help="synthetic frames per second")
    source.add_argument("--simulate-file", help="loop a raw JSONL replay file; no CAN bus transmission")
    parser.add_argument("--replay-rate", type=float, default=50,
                        help="raw replay frames per second with --simulate-file (default: 50)")
    parser.add_argument("--queue-size", type=int, default=1000)
    parser.add_argument("--window-size", type=int, default=256,
                        help="maximum frames in one TCP transaction (default: 256)")
    parser.add_argument("--batch-size", type=int, default=256,
                        help="maximum frames in one RAM capture batch")
    parser.add_argument("--flush-ms", type=float, default=20,
                        help="maximum capture batching delay")
    parser.add_argument("--status-interval", type=float, default=5,
                        help="seconds between buffer status log lines (default: 5)")
    args = parser.parse_args()
    if (args.canmanager_socket is None and args.interface is None and
            args.simulate_rate is None and args.simulate_file is None):
        parser.error("provide --canmanager-socket, --interface, --simulate-rate, or --simulate-file")
    if args.canmanager_socket is not None:
        if sys.platform != "linux":
            parser.error("--canmanager-socket requires Linux")
        if not args.canmanager_socket or "\0" in args.canmanager_socket:
            parser.error("provide a nonempty socket name without NUL characters")
    if (args.queue_size < 1 or args.window_size < 1 or args.batch_size < 1 or
            args.flush_ms <= 0 or args.status_interval <= 0 or
            args.replay_rate <= 0 or
            (args.simulate_rate is not None and args.simulate_rate <= 0)):
        parser.error("queue, batch, window, flush, and simulation values must be positive")
    if args.simulate_file:
        try:
            load_simulation_frames(args.simulate_file)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    try:
        # Start the concurrent pipeline after validating the chosen input.
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
