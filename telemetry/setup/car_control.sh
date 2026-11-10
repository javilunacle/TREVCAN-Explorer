#!/usr/bin/env bash
set -Eeuo pipefail

unit="trevcan-telemetry-car.service"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

state_json() {
    local active enabled state
    active="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
    if [[ "$active" == "active" ]]; then
        state="running"
    elif [[ "$active" == "inactive" || "$active" == "failed" ]]; then
        state="idle"
    else
        state="unknown"
    fi
    printf '{"ok":true,"state":"%s","service":"%s","boot_enabled":"%s"}\n' \
        "$state" "$active" "$enabled"
}

action="${SSH_ORIGINAL_COMMAND:-${1:-status}}"
case "$action" in
    status)
        state_json
        ;;
    start)
        systemctl --user start "$unit"
        state_json
        ;;
    stop)
        systemctl --user stop "$unit"
        sync
        state_json
        ;;
    *)
        printf '{"ok":false,"state":"error","error":"unsupported action"}\n'
        exit 64
        ;;
esac
