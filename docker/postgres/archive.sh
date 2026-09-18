#!/bin/sh
# Publish complete WAL atomically; an existing segment must contain identical bytes.
set -eu
source_file=$1
segment=$2
case "$segment" in ''|*[!A-Za-z0-9._-]*) exit 1;; esac
umask 077
temporary=$(mktemp /backup/archive/.archive.XXXXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
cp "$source_file" "$temporary"
sync -f "$temporary"
if ! ln "$temporary" "/backup/archive/$segment" 2>/dev/null; then
  cmp -s "$temporary" "/backup/archive/$segment" || exit 1
fi
sync -f /backup/archive
