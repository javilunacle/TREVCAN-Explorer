"""Render importable dashboard drafts for file provisioning on the telemetry Pi."""

import json
from pathlib import Path

try:
    from .dashboard_specs import organized_dashboards
except ImportError:  # Direct script execution on the telemetry Pi.
    from dashboard_specs import organized_dashboards


DIRECTORY = Path(__file__).resolve().parent
SOURCE_FILES = (
    DIRECTORY / "trevcan-can-explorer.draft.json",
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
    written = []
    for source in SOURCE_FILES:
        dashboard = replace_datasource(json.loads(source.read_text(encoding="utf-8")))
        destination = OUTPUT_DIRECTORY / source.name.replace(".draft", "")
        destination.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
        written.append(destination)
    for filename, dashboard in organized_dashboards():
        destination = OUTPUT_DIRECTORY / filename
        destination.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
        written.append(destination)
    expected = {path.name for path in written}
    for stale in OUTPUT_DIRECTORY.glob("*.json"):
        if stale.name not in expected:
            stale.unlink()
    return written


if __name__ == "__main__":
    for path in render():
        print(path)
