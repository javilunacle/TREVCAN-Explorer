#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this installer as the telemetry user, not as root." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
nginx_source="$script_dir/nginx/trevcan-telemetry.conf"
nginx_target="/etc/nginx/sites-available/trevcan-telemetry"
nginx_enabled="/etc/nginx/sites-enabled/trevcan-telemetry"

[[ -r "$nginx_source" ]] || {
    echo "Missing $nginx_source" >&2
    exit 1
}

echo "Installing mDNS and the TREVCAN friendly-URL proxy..."
sudo apt-get update
sudo apt-get install -y avahi-daemon nginx

sudo hostnamectl set-hostname telemetrypi
sudo install -m 0644 "$nginx_source" "$nginx_target"
sudo ln -sfn "$nginx_target" "$nginx_enabled"
sudo nginx -t
sudo systemctl enable --now avahi-daemon nginx
sudo systemctl restart avahi-daemon
sudo systemctl reload nginx

echo
echo "Friendly telemetry addresses are ready:"
echo "  Tools:   http://telemetrypi.local"
echo "  Grafana: http://telemetrypi.local:3000/dashboards"
echo
echo "The shell prompt will show the new hostname on the next login."
