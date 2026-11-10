#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
env_file="$repo_root/.env.telemetry"
python_bin="$repo_root/.venv/bin/python"

[[ -r "$env_file" ]] || {
    echo "Missing $env_file. Configure the telemetry Pi first." >&2
    exit 1
}
[[ -x "$python_bin" ]] || {
    echo "Missing $python_bin. Create the telemetry virtual environment first." >&2
    exit 1
}

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

cd "$repo_root"
exec "$python_bin" -m telemetry.scenario_runner --verify "$@"
