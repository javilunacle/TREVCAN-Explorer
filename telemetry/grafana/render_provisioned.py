"""Render importable dashboard drafts for file provisioning on the telemetry Pi."""

import json
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
SOURCE_FILES = (
    DIRECTORY / "trevcan-can-explorer.draft.json",
    DIRECTORY / "trevcan-systems.draft.json",
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
    return written


if __name__ == "__main__":
    for path in render():
        print(path)

