#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this setup as the telemetry user, not with sudo." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
car_host="${1:-${TELEMETRY_CAR_HOST:-192.168.0.145}}"
car_user="${2:-${TELEMETRY_CAR_USER:-trevor}}"
target="$car_user@$car_host"
config_dir="$HOME/.config/trevcan"
identity="$config_dir/control_ed25519"
known_hosts="$config_dir/control_known_hosts"
scan_file="$(mktemp)"
temporary_env=""
remote_dir=".cache/trevcan-control-setup"
trap 'rm -f "$scan_file" ${temporary_env:+"$temporary_env"}' EXIT

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

env_file="$repo_root/.env.telemetry"
[[ -r "$env_file" ]] || {
    echo "Missing $env_file; create it from .env.telemetry.example first." >&2
    exit 1
}
control_token=""
set +u
# shellcheck disable=SC1090
source "$env_file"
set -u
control_token="${TELEMETRY_CONTROL_TOKEN:-}"
if [[ ${#control_token} -lt 24 || "$control_token" == replace-* ]]; then
    control_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    temporary_env="$(mktemp)"
    grep -v '^TELEMETRY_CONTROL_TOKEN=' "$env_file" > "$temporary_env" || true
    printf "\nTELEMETRY_CONTROL_TOKEN='%s'\n" "$control_token" >> "$temporary_env"
    install -m 0600 "$temporary_env" "$env_file"
    echo "A separate operator token was added to $env_file."
fi

echo "Verifying the restricted connection..."
ssh -i "$identity" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o "UserKnownHostsFile=$known_hosts" -o StrictHostKeyChecking=yes "$target" status
echo "Restricted car control is ready."
