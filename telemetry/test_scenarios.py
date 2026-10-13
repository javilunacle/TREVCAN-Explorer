"""Tests for the finite synthetic scenario definitions."""

import tempfile
import unittest
from pathlib import Path

from .server import DBCDecoder
from .transforms import SessionRegistry, TransformEngine
from .scenario_runner import DBC_DIRECTORY, FrameFactory, build_scenarios


POLICY = Path(__file__).with_name("transform_policies.json")
BUS_DBCS = {
    "can0": [
        DBC_DIRECTORY / "BMS-Firmware-RTOS-Complete.dbc",
        DBC_DIRECTORY / "hvc.dbc",
        DBC_DIRECTORY / "VCU.dbc",
        DBC_DIRECTORY / "BMS-Inverter-Only.dbc",
        DBC_DIRECTORY / "Baby_MOBO.dbc",
    ],
    "can1": [
        DBC_DIRECTORY / "DAQ-Firmware.dbc",
        DBC_DIRECTORY / "VCU.dbc",
        DBC_DIRECTORY / "can9-database-01.09.dbc",
    ],
}


class ScenarioDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.factory = FrameFactory()
        cls.scenarios = build_scenarios(cls.factory)
        cls.decoder = DBCDecoder(bus_paths=BUS_DBCS)

    def decoded_events(self, name):
        result = []
        base = 1_000_000_000
        for seq, event in enumerate(self.scenarios[name].events, 1):
            item = event.frame
            frame = {
                "type": "frame", "car_id": f"scenario-{name}", "seq": seq,
                "timestamp_ns": base + round(event.offset_seconds * 1_000_000_000),
                "bus": item["bus"], "can_id": item["can_id"],
                "is_extended": item["is_extended"],
                "is_remote": item["is_remote"], "is_fd": item["is_fd"],
                "is_error": item["is_error"], "dlc": item["dlc"],
                "data": item["data"],
            }
            result.append((frame, self.decoder.decode(frame)))
        return result

    def test_intentional_messages_decode_with_receiver_bus_priority(self):
        for name in ("speed-step", "electrical-polarity", "temperature-spike",
                     "gg-valid", "gg-invalid", "stale-electrical-pair",
                     "hvc-faults"):
            with self.subTest(name=name):
                self.assertTrue(all(decoded is not None
                                    for _, decoded in self.decoded_events(name)))

    def test_decode_mismatch_remains_undecoded(self):
        self.assertIsNone(self.decoded_events("decode-mismatch")[0][1])

    def test_all_scenario_exercises_transforms_and_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions.json"
            SessionRegistry(sessions).start(now_ns=1_000_000_000)
            engine = TransformEngine(POLICY, sessions)
            plan = engine.prepare(self.decoded_events("all"))
            body = "\n".join(plan.lines)
            transform_outputs = (
                "Vehicle_Speed_mps", "Distance_m", "Pack_Power_W",
                "Energy_Charged_Wh", "Energy_Used_Wh",
                "Accumulator_Temp_Rise_C_per_s", "temperature_spike",
                "IMU_Alignment_Valid", "Longitudinal_Accel_g",
            )
            for expected in transform_outputs:
                self.assertIn(expected, body)

    def test_invalid_alignment_and_stale_pair_are_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions.json"
            SessionRegistry(sessions).start(now_ns=1_000_000_000)
            for name in ("gg-invalid", "stale-electrical-pair"):
                with self.subTest(name=name):
                    engine = TransformEngine(POLICY, sessions)
                    body = "\n".join(engine.prepare(self.decoded_events(name)).lines)
                    for forbidden in self.scenarios[name].forbidden:
                        self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
