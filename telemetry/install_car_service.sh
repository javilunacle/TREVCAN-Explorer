#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this installer as the car user, not with sudo." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
template="$script_dir/systemd/trevcan-telemetry-car.service.in"
app_root="$HOME/.local/share/trevcan"
package_dir="$app_root/telemetry"
config_dir="$HOME/.config/trevcan"
config_path="$config_dir/car.env"
unit_dir="$HOME/.config/systemd/user"
unit_path="$unit_dir/trevcan-telemetry-car.service"
enable_at_boot=false
server_host="${TELEMETRY_SERVER_HOST:-192.168.0.136}"
host_supplied=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --enable-at-boot)
            enable_at_boot=true
            ;;
        --help|-h)
            echo "Usage: $0 [--enable-at-boot] [telemetry-server-host]"
            exit 0
            ;;
        --*)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
        *)
            if [[ "$host_supplied" == true ]]; then
                echo "Only one telemetry server address may be supplied." >&2
                exit 2
            fi
            server_host="$1"
            host_supplied=true
            ;;
    esac
    shift
done

if [[ ! "$server_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "Invalid telemetry server hostname or address: $server_host" >&2
    exit 1
fi

python_bin="${TREVCAN_CAR_PYTHON:-}"
for candidate in \
    "$python_bin" \
    "$repo_root/.venv/bin/python" \
    "$(command -v python3 || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        python_bin="$candidate"
        break
    fi
done
[[ -x "$python_bin" ]] || {
    echo "No usable Python interpreter found." >&2
    exit 1
}
# The default CANManager socket input uses only the Python standard library.
# python-can is needed only when manually selecting direct --interface capture.

token="${TELEMETRY_TOKEN:-}"
if [[ -z "$token" && -r "$config_path" ]]; then
    # shellcheck disable=SC1090
    source "$config_path"
    token="${TELEMETRY_TOKEN:-}"
fi
if [[ -z "$token" ]]; then
    read -rsp 'Shared car-telemetry token: ' token
    echo
fi
if [[ ! "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
    echo "The telemetry token may contain only letters, digits, and ._~+/=-" >&2
    exit 1
fi

mkdir -p "$package_dir" "$config_dir" "$unit_dir"
install -m 0644 "$script_dir/__init__.py" "$package_dir/__init__.py"
install -m 0644 "$script_dir/common.py" "$package_dir/common.py"
install -m 0644 "$script_dir/buffers.py" "$package_dir/buffers.py"
install -m 0644 "$script_dir/car.py" "$package_dir/car.py"
install -m 0644 "$script_dir/canmanager_input.py" "$package_dir/canmanager_input.py"

temporary_env="$(mktemp)"
temporary_unit="$(mktemp)"
trap 'rm -f "$temporary_env" "$temporary_unit"' EXIT
{
    printf 'TELEMETRY_TOKEN=%s\n' "$token"
    printf 'TELEMETRY_SERVER_HOST=%s\n' "$server_host"
    printf 'TELEMETRY_SERVER_PORT=8765\n'
} > "$temporary_env"
install -m 0600 "$temporary_env" "$config_path"

sed -e "s|__PYTHON__|$python_bin|g" "$template" > "$temporary_unit"
install -m 0644 "$temporary_unit" "$unit_path"

sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
if [[ "$enable_at_boot" == true ]]; then
    systemctl --user enable trevcan-telemetry-car.service
    if systemctl --user is-active --quiet trevcan-telemetry-car.service; then
        systemctl --user restart trevcan-telemetry-car.service
    elif pgrep -af '[t]elemetry.car' >/dev/null 2>&1; then
        echo "Installed and enabled, but a manual telemetry.car sender is already running."
        echo "Stop the manual sender, then start the service."
    else
        systemctl --user start trevcan-telemetry-car.service
    fi
else
    systemctl --user disable trevcan-telemetry-car.service >/dev/null 2>&1 || true
    echo "Boot startup is disabled. TREVCAN Telemetry Tools starts this service for a test."
    if systemctl --user is-active --quiet trevcan-telemetry-car.service; then
        echo "The service was already running and was not interrupted by this installer."
    fi
fi

echo "Car sender service installed."
echo "Buffer: bounded RAM only; disconnected data is discarded."
echo "Status: systemctl --user status trevcan-telemetry-car.service"
echo "Logs:   journalctl --user -u trevcan-telemetry-car.service -f"
