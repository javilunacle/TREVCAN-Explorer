#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this installer as the telemetry user, not with sudo." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
template="$script_dir/systemd/trevcan-telemetry-control.service.in"
unit_dir="$HOME/.config/systemd/user"
unit_path="$unit_dir/trevcan-telemetry-control.service"
env_file="$repo_root/.env.telemetry"
start_now=false

if [[ "${1:-}" == "--start-now" ]]; then
    start_now=true
    shift
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $0 [--start-now]"
    exit 0
fi
[[ $# -eq 0 ]] || { echo "Usage: $0 [--start-now]" >&2; exit 2; }

[[ -x "$repo_root/.venv/bin/python" ]] || {
    echo "Missing $repo_root/.venv/bin/python" >&2
    exit 1
}
[[ -r "$env_file" ]] || { echo "Missing $env_file" >&2; exit 1; }
control_token=""
set +u
# shellcheck disable=SC1090
source "$env_file"
set -u
control_token="${TELEMETRY_CONTROL_TOKEN:-}"
[[ ${#control_token} -ge 24 && "$control_token" != replace-* ]] || {
    echo "Set a non-example TELEMETRY_CONTROL_TOKEN of at least 24 characters." >&2
    exit 1
}
[[ -r "$HOME/.config/trevcan/control_ed25519" ]] || {
    echo "Run bash telemetry/setup_control_pi.sh first." >&2
    exit 1
}
[[ -r "$HOME/.config/trevcan/control_known_hosts" ]] || {
    echo "Run bash telemetry/setup_control_pi.sh first." >&2
    exit 1
}

mkdir -p "$unit_dir"
temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
sed -e "s|__REPO_ROOT__|$repo_root|g" -e "s|__HOME__|$HOME|g" \
    "$template" > "$temporary"
install -m 0644 "$temporary" "$unit_path"
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user disable trevcan-telemetry-control.service >/dev/null 2>&1 || true

if [[ "$start_now" == true ]]; then
    systemctl --user restart trevcan-telemetry-control.service
    echo "Operations page started for this boot."
else
    echo "Operations page installed but not started and disabled at boot."
fi
echo "Start:  systemctl --user start trevcan-telemetry-control.service"
echo "Stop:   systemctl --user stop trevcan-telemetry-control.service"
echo "Open:   http://192.168.0.136:8080"
