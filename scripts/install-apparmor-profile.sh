#!/bin/sh
set -eu

profile_source="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/security/apparmor/hootbeatbot-chromium"
profile_target=/etc/apparmor.d/hootbeatbot-chromium

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run this installer as root." >&2
    exit 1
fi

command -v apparmor_parser >/dev/null 2>&1 || {
    printf '%s\n' "apparmor_parser is required." >&2
    exit 1
}

install -o root -g root -m 0644 "$profile_source" "$profile_target"
apparmor_parser -r "$profile_target"
printf '%s\n' "Loaded AppArmor profile: hootbeatbot-chromium"
