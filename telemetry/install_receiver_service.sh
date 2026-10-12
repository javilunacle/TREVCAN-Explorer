#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this installer as the telemetry user, not with sudo." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
template="$script_dir/systemd/trevcan-telemetry-receiver.service.in"
env_file="$repo_root/.env.telemetry"
python_bin="$repo_root/.venv/bin/python"
unit_dir="$HOME/.config/systemd/user"
unit_path="$unit_dir/trevcan-telemetry-receiver.service"
enable_at_boot=false

if [[ "${1:-}" == "--enable-at-boot" ]]; then
    enable_at_boot=true
    shift
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $0 [--enable-at-boot]"
    exit 0
fi
[[ $# -eq 0 ]] || {
    echo "Usage: $0 [--enable-at-boot]" >&2
    exit 2
}

[[ -r "$env_file" ]] || {
    echo "Missing $env_file; configure .env.telemetry first." >&2
    exit 1
}
[[ -x "$python_bin" ]] || {
    echo "Missing $python_bin; create the telemetry virtual environment first." >&2
    exit 1
}
command -v docker >/dev/null 2>&1 || {
    echo "Docker is not available for this user." >&2
    exit 1
}

mkdir -p "$unit_dir"
temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
sed \
    -e "s|__REPO_ROOT__|$repo_root|g" \
    -e "s|__HOME__|$HOME|g" \
    "$template" > "$temporary"
install -m 0644 "$temporary" "$unit_path"

sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
if [[ "$enable_at_boot" == true ]]; then
    systemctl --user enable trevcan-telemetry-receiver.service
    if systemctl --user is-active --quiet trevcan-telemetry-receiver.service; then
        systemctl --user restart trevcan-telemetry-receiver.service
    elif ss -ltn 2>/dev/null | grep -q ':8765 '; then
        echo "Installed and enabled, but port 8765 is already in use."
        echo "Stop the foreground receiver, then start the service."
    else
        systemctl --user start trevcan-telemetry-receiver.service
    fi
else
    systemctl --user disable trevcan-telemetry-receiver.service >/dev/null 2>&1 || true
    echo "Boot startup is disabled. The Operations page starts this service for a test."
    if systemctl --user is-active --quiet trevcan-telemetry-receiver.service; then
        echo "The service was already running and was not interrupted by this installer."
    fi
fi

echo "Receiver service installed."
echo "Status: systemctl --user status trevcan-telemetry-receiver.service"
echo "Logs:   journalctl --user -u trevcan-telemetry-receiver.service -f"
