#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

env_file="${TREVCAN_ENV_FILE:-$repo_root/.env.telemetry}"
if [[ ! -r "$env_file" ]]; then
    echo "Missing $env_file; copy .env.telemetry.example and set its secrets." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

required=(TELEMETRY_TOKEN INFLUXDB_TOKEN INFLUXDB_ADMIN_USERNAME
          INFLUXDB_ADMIN_PASSWORD GRAFANA_ADMIN_USERNAME GRAFANA_ADMIN_PASSWORD)
for name in "${required[@]}"; do
    if [[ -z "${!name:-}" || "${!name}" == replace-with-* ]]; then
        echo "$name is missing or still has its example value in $env_file" >&2
        exit 1
    fi
done

python_bin="${TREVCAN_PYTHON:-$repo_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    echo "Missing $python_bin; create the venv and install cantools first." >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed or is not on PATH." >&2
    exit 1
fi

data_dir="${TREVCAN_DATA_DIR:-$HOME/trevcan-data}"
mkdir -p "$data_dir"

"$python_bin" telemetry/grafana/render_provisioned.py
docker compose -f docker-compose.telemetry-pi.yml up -d

influx_url='http://127.0.0.1:8086/api/v2/write?org=docs&bucket=home&precision=ns'
exec "$python_bin" -m telemetry.server \
    --host 0.0.0.0 \
    --port 8765 \
    --db "$data_dir/telemetry-server.sqlite3" \
    --token "$TELEMETRY_TOKEN" \
    --dbc "$repo_root/webserver/backend/dbc_files/BMS-Firmware-RTOS-Complete.dbc" \
    --dbc "$repo_root/webserver/backend/dbc_files/hvc.dbc" \
    --dbc "$repo_root/webserver/backend/dbc_files/BMS-Inverter-Only.dbc" \
    --dbc "$repo_root/webserver/backend/dbc_files/master.dbc" \
    --dbc "$repo_root/webserver/backend/dbc_files/Baby_MOBO.dbc" \
    --influx-url "$influx_url" \
    --influx-token "$INFLUXDB_TOKEN"
