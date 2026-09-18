#!/bin/sh
set -eu
# The official entrypoint starts as root, then drops to the image's postgres uid.
mkdir -p /backup/archive
chown postgres:postgres /backup/archive
chmod 700 /backup/archive
exec /usr/local/bin/docker-entrypoint.sh "$@"
