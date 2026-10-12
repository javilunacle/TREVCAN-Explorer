#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this installer as the car user, not with sudo." >&2
    exit 1
fi
if [[ $# -ne 1 ]]; then
    echo "Usage: $0 /path/to/control_ed25519.pub" >&2
    exit 2
fi
if [[ ! "$USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "Unsafe account name: $USER" >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
public_key_path="$1"
source_control="$script_dir/car_control.sh"
installed_dir="$HOME/.local/share/trevcan"
installed_control="$installed_dir/car_control.sh"
authorized_keys="$HOME/.ssh/authorized_keys"
unit="$HOME/.config/systemd/user/trevcan-telemetry-car.service"
marker="trevcan-telemetry-control"

[[ -r "$public_key_path" ]] || {
    echo "Cannot read public key: $public_key_path" >&2
    exit 1
}
[[ -r "$source_control" ]] || {
    echo "Missing $source_control" >&2
    exit 1
}
[[ -r "$unit" ]] || {
    echo "Install the car telemetry service first: bash telemetry/install_car_service.sh" >&2
    exit 1
}

read -r key_type key_body _ < "$public_key_path"
case "$key_type" in
    ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ;;
    *) echo "Unsupported SSH key type: $key_type" >&2; exit 1 ;;
esac
[[ "$key_body" =~ ^[A-Za-z0-9+/=]+$ ]] || {
    echo "The public key is malformed." >&2
    exit 1
}

mkdir -p "$installed_dir" "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
install -m 0755 "$source_control" "$installed_control"

temporary_keys="$(mktemp)"
temporary_sudoers="$(mktemp)"
trap 'rm -f "$temporary_keys" "$temporary_sudoers"' EXIT
if [[ -f "$authorized_keys" ]]; then
    grep -vF "$marker" "$authorized_keys" > "$temporary_keys" || true
fi
printf 'command="%s",no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding %s %s %s\n' \
    "$installed_control" "$key_type" "$key_body" "$marker" >> "$temporary_keys"
install -m 0600 "$temporary_keys" "$authorized_keys"

printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block\n' \
    "$USER" > "$temporary_sudoers"
visudo_bin="$(command -v visudo || true)"
[[ -n "$visudo_bin" ]] || visudo_bin="/usr/sbin/visudo"
[[ -x "$visudo_bin" ]] || {
    echo "visudo is required." >&2
    exit 1
}
sudo "$visudo_bin" -cf "$temporary_sudoers"
sudo install -m 0440 "$temporary_sudoers" "/etc/sudoers.d/trevcan-telemetry-control-$USER"
sudo loginctl enable-linger "$USER"

echo "Restricted telemetry-control key installed."
echo "It can only query/start/stop telemetry or request an orderly shutdown."
echo "The telemetry sender remains disabled at boot."
