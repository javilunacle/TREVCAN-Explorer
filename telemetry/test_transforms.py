"""Deterministic tests for timestamp-based live telemetry transforms."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .server import DBCDecoder, RawStore, export_forever
from .simulate import DBC_DIRECTORY, generate_frames
from .transforms import SessionRegistry, TransformEngine


POLICY = Path(__file__).with_name("transform_policies.json")


def decoded(seq, timestamp_ns, dbc, message, signals, *, bus="can0", car="car-1"):
    frame = {
        "type": "frame", "car_id": car, "seq": seq,
        "timestamp_ns": timestamp_ns, "bus": bus,
        "can_id": seq, "is_extended": False, "is_remote": False,
        "is_fd": False, "is_error": False, "dlc": 8, "data": b"\0" * 8,
    }
    return frame, (dbc, message, signals)


class SessionRegistryTests(unittest.TestCase):
    def test_sessions_map_delayed_frames_to_the_test_that_captured_them(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SessionRegistry(Path(directory) / "sessions.json")
            first = registry.start(now_ns=1_000)
            registry.end(now_ns=2_000)
            second = registry.start(now_ns=3_000)
            self.assertEqual(registry.session_for(1_500), first["id"])
            self.assertEqual(registry.session_for(3_500), second["id"])
            self.assertEqual(registry.session_for(2_500), "unassigned")


class TransformEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.sessions = Path(self.temporary.name) / "sessions.json"
        SessionRegistry(self.sessions).start(now_ns=1_000_000_000)
        self.engine = TransformEngine(POLICY, self.sessions)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def only_state(plan):
        return next(iter(plan.states.values()))

    def test_irregular_speed_updates_use_source_time_for_distance_and_derivative(self):
        inputs = [
            decoded(1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 0.0}),
            decoded(2, 1_100_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 22.3693629205}),
            decoded(3, 1_200_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 22.3693629205}),
        ]
        plan = self.engine.prepare(inputs)
        state = self.only_state(plan)
        self.assertAlmostEqual(state["speed"]["distance_m"], 1.5, places=5)
        self.assertTrue(any("signal=Vehicle_Accel_From_Speed_mps2" in line
                            for line in plan.lines))

    def test_stationary_speed_jitter_does_not_accumulate_distance_or_acceleration(self):
        inputs = [
            decoded(1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 0.01}),
            decoded(2, 1_100_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 0.0}),
            decoded(3, 1_200_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 0.1}),
        ]
        plan = self.engine.prepare(inputs)
        speed = self.only_state(plan)["speed"]
        self.assertEqual(speed["distance_m"], 0.0)
        self.assertEqual(speed["last_v"], 0.0)
        speed_lines = [line for line in plan.lines
                       if "signal=Vehicle_Speed_mps" in line]
        acceleration_lines = [line for line in plan.lines
                              if "signal=Vehicle_Accel_From_Speed_mps2" in line]
        self.assertTrue(speed_lines)
        self.assertTrue(all(" value=0.0," in line for line in speed_lines))
        self.assertTrue(acceleration_lines)
        self.assertTrue(all(" value=0.0," in line for line in acceleration_lines))

    def test_speed_above_deadband_is_preserved(self):
        plan = self.engine.prepare([
            decoded(1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 1.0}),
        ])
        speed = self.only_state(plan)["speed"]
        self.assertAlmostEqual(speed["last_v"], 0.44704)

    def test_vehicle_speed_policy_matches_the_real_vcu_dbc_on_can1(self):
        dbc_path = DBC_DIRECTORY / "VCU.dbc"
        generated = next(
            frame for frame in generate_frames([dbc_path], cycles=1)
            if frame["message"] == "VCU_Summary")
        frame = {
            "type": "frame", "car_id": "car-1", "seq": 1,
            "timestamp_ns": 1_100_000_000, "bus": "can1",
            "can_id": generated["can_id"],
            "is_extended": generated["is_extended"], "is_remote": False,
            "is_fd": generated["is_fd"], "is_error": False,
            "dlc": generated["dlc"], "data": bytes.fromhex(generated["data"]),
        }
        decoder = DBCDecoder(bus_paths={"can1": [dbc_path]})
        plan = self.engine.prepare([(frame, decoder.decode(frame))])
        self.assertTrue(any("signal=Vehicle_Speed_mps" in line for line in plan.lines))

    def test_positive_current_accumulates_charge_and_negative_accumulates_use(self):
        inputs = [
            decoded(1, 1_000_000_000, "hvc.dbc", "HVC_IO_Current",
                    {"HVC_Pack_Current_A": 10.0}),
            decoded(2, 1_010_000_000, "hvc.dbc", "HVC_IO_VSense",
                    {"HVC_Batt_Voltage_V": 400.0}),
            decoded(3, 1_990_000_000, "hvc.dbc", "HVC_IO_VSense",
                    {"HVC_Batt_Voltage_V": 400.0}),
            decoded(4, 2_000_000_000, "hvc.dbc", "HVC_IO_Current",
                    {"HVC_Pack_Current_A": 10.0}),
            decoded(5, 2_990_000_000, "hvc.dbc", "HVC_IO_VSense",
                    {"HVC_Batt_Voltage_V": 400.0}),
            decoded(6, 3_000_000_000, "hvc.dbc", "HVC_IO_Current",
                    {"HVC_Pack_Current_A": -10.0}),
            decoded(7, 3_990_000_000, "hvc.dbc", "HVC_IO_VSense",
                    {"HVC_Batt_Voltage_V": 400.0}),
            decoded(8, 4_000_000_000, "hvc.dbc", "HVC_IO_Current",
                    {"HVC_Pack_Current_A": -10.0}),
        ]
        electrical = self.only_state(self.engine.prepare(inputs))["electrical"]
        self.assertGreater(electrical["charge_added_ah"], 0)
        self.assertGreater(electrical["charge_removed_ah"], 0)
        self.assertGreater(electrical["energy_charged_wh"], 0)
        self.assertGreater(electrical["energy_used_wh"], 0)
        self.assertLess(abs(electrical["net_energy_wh"]), 0.02)

    def test_temperature_spike_emits_a_rate_limited_diagnostic_event(self):
        inputs = [
            decoded(1, 1_000_000_000, "hvc.dbc", "HVC_ACC_Summary",
                    {"HVC_Acc_Temp_Max_C": 20.0}),
            decoded(2, 1_500_000_000, "hvc.dbc", "HVC_ACC_Summary",
                    {"HVC_Acc_Temp_Max_C": 41.0}),
            decoded(3, 1_700_000_000, "hvc.dbc", "HVC_ACC_Summary",
                    {"HVC_Acc_Temp_Max_C": 19.0}),
        ]
        plan = self.engine.prepare(inputs)
        warnings = [line for line in plan.lines if line.startswith("telemetry_event,")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("rule=temperature_spike", warnings[0])

    def test_gg_points_require_valid_fresh_alignment(self):
        imu = decoded(
            1, 1_100_000_000, "can9-database-01.09.dbc", "ImuData",
            {"ImuValid": 1, "AccelerationX": 9.80665, "AccelerationY": -4.903325},
            bus="can1")
        self.assertFalse(any(line.startswith("vehicle_dynamics,")
                             for line in self.engine.prepare([imu]).lines))
        aligned = decoded(
            2, 1_050_000_000, "can9-database-01.09.dbc", "ImuAlign",
            {"AlignStatus": 3, "AlignXYError": 0, "AlignZError": 0,
             "AlignError": 0}, bus="can1")
        plan = self.engine.prepare([aligned, imu])
        dynamics = next(line for line in plan.lines
                        if line.startswith("vehicle_dynamics,"))
        self.assertIn("longitudinal_g=1.0", dynamics)
        self.assertIn("lateral_g=-0.5", dynamics)

    def test_prepare_is_repeatable_until_the_plan_is_committed(self):
        inputs = [
            decoded(1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 10.0}),
            decoded(2, 1_100_000_000, "VCU.dbc", "VCU_Summary",
                    {"VCU_Speed_MPH": 20.0}),
        ]
        first = self.engine.prepare(inputs)
        retry = self.engine.prepare(inputs)
        self.assertEqual(first.lines, retry.lines)
        self.assertEqual(first.states, retry.states)

    def test_transform_state_round_trips_through_receiver_database(self):
        database = Path(self.temporary.name) / "receiver.sqlite3"
        store = RawStore(database)
        try:
            plan = self.engine.prepare([
                decoded(1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                        {"VCU_Speed_MPH": 10.0})])
            frame = plan.states and decoded(
                1, 1_000_000_000, "VCU.dbc", "VCU_Summary",
                {"VCU_Speed_MPH": 10.0})[0]
            frame["data"] = "0000000000000000"
            store.commit_frame(frame)
            store.finish_export(store.pending(), plan.persisted_states())
            key = next(iter(plan.states))
            self.assertEqual(store.load_transform_state(*key), plan.states[key])
            self.assertEqual(store.status()["pending_influx"], 0)
            self.assertIsInstance(json.loads(plan.persisted_states()[0][3]), dict)
        finally:
            store.close()


class _SpeedDecoder:
    def decode(self, frame):
        return "VCU.dbc", "VCU_Summary", {"VCU_Speed_MPH": float(frame["seq"])}


class TransformExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_checkpoints_transform_only_after_influx_accepts_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions.json"
            session = SessionRegistry(sessions).start(now_ns=1_000_000_000)
            store = RawStore(root / "receiver.sqlite3")
            frame = decoded(
                1, 1_100_000_000, "VCU.dbc", "VCU_Summary",
                {"VCU_Speed_MPH": 1.0})[0]
            frame["data"] = "0000000000000000"
            store.commit_frame(frame)
            engine = TransformEngine(POLICY, sessions, store.load_transform_state)
            stop = asyncio.Event()

            with patch("telemetry.server.post_influx"):
                task = asyncio.create_task(export_forever(
                    store, _SpeedDecoder(), "http://unused", "token", stop,
                    interval_seconds=0.001, transformer=engine))
                for _ in range(100):
                    if store.status()["pending_influx"] == 0:
                        break
                    await asyncio.sleep(0.01)
                stop.set()
                await task

            key = (session["id"], "car-1", engine.STATE_NAME)
            self.assertEqual(store.status()["pending_influx"], 0)
            self.assertIsNotNone(store.load_transform_state(*key))
            store.close()

    async def test_failed_influx_write_retains_raw_and_does_not_advance_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions.json"
            session = SessionRegistry(sessions).start(now_ns=1_000_000_000)
            store = RawStore(root / "receiver.sqlite3")
            frame = decoded(
                1, 1_100_000_000, "VCU.dbc", "VCU_Summary",
                {"VCU_Speed_MPH": 1.0})[0]
            frame["data"] = "0000000000000000"
            store.commit_frame(frame)
            engine = TransformEngine(POLICY, sessions, store.load_transform_state)
            stop = asyncio.Event()

            with patch("telemetry.server.post_influx", side_effect=RuntimeError("offline")):
                task = asyncio.create_task(export_forever(
                    store, _SpeedDecoder(), "http://unused", "token", stop,
                    interval_seconds=0.001, transformer=engine))
                await asyncio.sleep(0.03)
                stop.set()
                await task

            key = (session["id"], "car-1", engine.STATE_NAME)
            self.assertEqual(store.status()["pending_influx"], 1)
            self.assertIsNone(store.load_transform_state(*key))
            store.close()


if __name__ == "__main__":
    unittest.main()
