#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this setup as the telemetry user, not with sudo." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
car_host="${1:-${TELEMETRY_CAR_HOST:-192.168.0.145}}"
car_user="${2:-${TELEMETRY_CAR_USER:-trevor}}"
target="$car_user@$car_host"
config_dir="$HOME/.config/trevcan"
identity="$config_dir/control_ed25519"
known_hosts="$config_dir/control_known_hosts"
scan_file="$(mktemp)"
remote_dir=".cache/trevcan-control-setup"
trap 'rm -f "$scan_file"' EXIT

[[ "$car_host" =~ ^[A-Za-z0-9.-]+$ ]] || {
    echo "Invalid car host: $car_host" >&2
    exit 2
}
[[ "$car_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
    echo "Invalid car user: $car_user" >&2
    exit 2
}

mkdir -p "$config_dir"
chmod 700 "$config_dir"
if [[ ! -f "$identity" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C trevcan-telemetry-control -f "$identity"
fi
chmod 600 "$identity"

echo "Reading the car Pi SSH host key from $car_host..."
ssh-keyscan -T 5 -t ed25519 "$car_host" > "$scan_file" 2>/dev/null
[[ -s "$scan_file" ]] || {
    echo "Could not read an Ed25519 SSH host key from $car_host." >&2
    exit 1
}
echo "Scanned car-Pi host-key fingerprint:"
ssh-keygen -lf "$scan_file"
echo "Compare it on the car Pi with:"
echo "  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
read -rp 'Does the fingerprint exactly match? [y/N] ' confirmed
[[ "$confirmed" =~ ^[Yy]$ ]] || {
    echo "Host key was not trusted; no connection settings were changed."
    exit 1
}
install -m 0600 "$scan_file" "$known_hosts"

ssh -o "UserKnownHostsFile=$known_hosts" -o StrictHostKeyChecking=yes \
    "$target" "mkdir -p '$remote_dir'"
scp -o "UserKnownHostsFile=$known_hosts" -o StrictHostKeyChecking=yes \
    "$identity.pub" "$script_dir/install_car_control.sh" "$script_dir/car_control.sh" \
    "$target:$remote_dir/"
ssh -t -o "UserKnownHostsFile=$known_hosts" -o StrictHostKeyChecking=yes \
    "$target" "bash '$remote_dir/install_car_control.sh' '$remote_dir/control_ed25519.pub'"

echo "Verifying the restricted connection..."
ssh -i "$identity" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o "UserKnownHostsFile=$known_hosts" -o StrictHostKeyChecking=yes "$target" status
echo "Restricted car control is ready."
