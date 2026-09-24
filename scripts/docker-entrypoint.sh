#!/bin/sh
set -eu

umask 077
mkdir -p /data/state /app/var/tmp
chmod 700 /data/state /app/var/tmp
if [ -f /app/.env ]; then
    chmod 600 /app/.env
fi
exec "$@"
