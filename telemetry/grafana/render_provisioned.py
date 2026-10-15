"""Render telemetry dashboards for file provisioning on the telemetry Pi."""

import json
import sys
from pathlib import Path

try:
    from .dashboard_specs import organized_dashboards
    from .live_dashboard import live_dashboard
except ImportError:  # Direct script execution on the telemetry Pi.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from telemetry.grafana.dashboard_specs import organized_dashboards
    from telemetry.grafana.live_dashboard import live_dashboard


DIRECTORY = Path(__file__).resolve().parent
SOURCE_FILES = (
    DIRECTORY / "trevcan-history-frames.json",
)
OUTPUT_DIRECTORY = DIRECTORY / "generated"


def replace_datasource(value):
    if isinstance(value, dict):
        return {key: replace_datasource(item) for key, item in value.items()
                if key != "__inputs"}
    if isinstance(value, list):
        return [replace_datasource(item) for item in value]
    if value == "${DS_INFLUXDB}":
        return "trevcan-influxdb"
    return value


def render():
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    # The receiver service intentionally runs with UMask=0077 because it reads
    # telemetry secrets.  Grafana is a different UID inside its container, so
    # provisioned dashboard files must explicitly remain world-readable.
    OUTPUT_DIRECTORY.chmod(0o755)
    written = []
    for source in SOURCE_FILES:
        dashboard = replace_datasource(json.loads(source.read_text(encoding="utf-8")))
        destination = OUTPUT_DIRECTORY / source.name
        destination.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
        destination.chmod(0o644)
        written.append(destination)
    dashboards = [("trevcan-live-telemetry.json", live_dashboard())]
    for filename, dashboard in organized_dashboards():
        dashboard["title"] = dashboard["title"].replace("Live Analysis", "Analysis").replace(
            "TREVCAN - ", "TREVCAN - Historical ")
        dashboard["tags"].append("historical")
        dashboards.append((filename, dashboard))
    for filename, dashboard in dashboards:
        destination = OUTPUT_DIRECTORY / filename
        destination.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
        destination.chmod(0o644)
        written.append(destination)
    expected = {path.name for path in written}
    for stale in OUTPUT_DIRECTORY.glob("*.json"):
        if stale.name not in expected:
            stale.unlink()
    return written


if __name__ == "__main__":
    for path in render():
        print(path)
