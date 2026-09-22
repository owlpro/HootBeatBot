#!/bin/sh
set -eu

umask 077
profile_path=${TELEGRAM_WEB_PROFILE_PATH:-/data/telegram-web-profile}
mkdir -p "$profile_path"
chmod 700 "$profile_path"
# Chromium creates nested profile state; tighten an existing mounted profile too.
find "$profile_path" -type d -exec chmod 700 {} +
find "$profile_path" -type f -exec chmod 600 {} +
exec "$@"
