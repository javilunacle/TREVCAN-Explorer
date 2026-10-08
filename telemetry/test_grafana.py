import json
import re
import unittest
from pathlib import Path

import cantools

from telemetry.grafana.dashboard_specs import organized_dashboards


class GrafanaDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dbc_directory = Path(__file__).resolve().parents[1] / "webserver/backend/dbc_files"
        databases = [
            cantools.database.load_file(path, strict=False)
            for path in dbc_directory.glob("*.dbc")
        ]
        cls.signal_names = {
            signal.name
            for database in databases
            for message in database.messages
            for signal in message.signals
        }
        cls.message_names = {
            message.name for database in databases for message in database.messages
        }

    def test_dashboard_names_and_uids_are_unique(self):
        dashboards = organized_dashboards()
        self.assertEqual(len(dashboards), 6)
        self.assertEqual(len({filename for filename, _ in dashboards}), len(dashboards))
        self.assertEqual(len({dashboard["uid"] for _, dashboard in dashboards}), len(dashboards))
        self.assertEqual(
            {dashboard["title"] for _, dashboard in dashboards},
            {
                "TREVCAN - Vehicle Overview",
                "TREVCAN - BMS",
                "TREVCAN - HVC",
                "TREVCAN - Inverter",
                "TREVCAN - VCU",
                "TREVCAN - MOBO",
            },
        )

    def test_every_panel_has_a_flux_target_and_fixed_datasource(self):
        for _, dashboard in organized_dashboards():
            self.assertTrue(dashboard["panels"])
            for panel in dashboard["panels"]:
                self.assertEqual(panel["datasource"]["uid"], "trevcan-influxdb")
                self.assertTrue(panel["targets"])
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"]["uid"], "trevcan-influxdb")
                    self.assertIn('from(bucket: "home")', target["query"])
                    self.assertIn('r._measurement == "can_signal"', target["query"])

    def test_explicit_signal_and_message_names_exist_in_repository_dbcs(self):
        missing_signals = set()
        missing_messages = set()
        for _, dashboard in organized_dashboards():
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    query = target["query"]
                    missing_signals.update(
                        name
                        for name in re.findall(r'r\.signal == "([^"]+)"', query)
                        if name not in self.signal_names
                    )
                    missing_messages.update(
                        name
                        for name in re.findall(r'r\.message == "([^"]+)"', query)
                        if name not in self.message_names
                    )
        self.assertEqual(missing_signals, set())
        self.assertEqual(missing_messages, set())

    def test_bms_dashboard_uses_a_single_module_selector(self):
        dashboard = dict(organized_dashboards())["trevcan-bms.json"]
        variables = dashboard["templating"]["list"]
        self.assertEqual(len(variables), 1)
        self.assertEqual(variables[0]["name"], "bms_module")
        self.assertEqual(variables[0]["query"], "0,1,2,3,4,5")

    def test_live_panels_bound_the_number_of_returned_points(self):
        for _, dashboard in organized_dashboards():
            for panel in dashboard["panels"]:
                query = panel["targets"][0]["query"]
                if panel["type"] == "timeseries":
                    self.assertIn("aggregateWindow(every: v.windowPeriod, fn: last", query)
                elif panel["type"] in {"stat", "bargauge"}:
                    self.assertIn("|> last()", query)

    def test_can_explorer_bounds_and_filters_the_combined_table(self):
        path = (Path(__file__).resolve().parent / "grafana" /
                "trevcan-can-explorer.draft.json")
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        table = dashboard["panels"][0]
        query = table["targets"][0]["query"]
        variables = {item["name"]: item for item in dashboard["templating"]["list"]}

        self.assertIn('|> range(start: -1m)', query)
        self.assertIn('|> limit(n: 500)', query)
        self.assertIn('"bus", "can_id"', query)
        self.assertIn('${dbc_file:regex}', query)
        self.assertIn('${can_bus:regex}', query)
        self.assertEqual(set(variables), {"dbc_file", "can_bus"})
        self.assertEqual(variables["dbc_file"]["allValue"], ".*")
        self.assertEqual(variables["can_bus"]["allValue"], ".*")
        organize = table["transformations"][0]
        self.assertEqual(organize["id"], "organize")
        self.assertEqual(organize["options"]["indexByName"]["dbc"], 0)
        self.assertEqual(organize["options"]["indexByName"]["message"], 1)


if __name__ == "__main__":
    unittest.main()
