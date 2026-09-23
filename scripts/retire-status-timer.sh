#!/bin/sh
# Retire the version 1 status timer and its records (Status v2 upgrade step). Safe to rerun.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
name=llm-gateway-status
# Name only units whose files exist; systemctl fails on a missing one.
set --
for unit in "$name.timer" "$name.service"; do
  [ ! -e "$units/$unit" ] || set -- "$@" "$unit"
done
if [ "$#" -gt 0 ]; then
  systemctl --user disable --now "$@"
  for unit in "$@"; do
    rm -f -- "$units/$unit"
    echo "disabled and removed $unit"
  done
  systemctl --user daemon-reload
else
  echo "no $name units in $units"
fi
for file in "$root/data/status/bootstrap.json" "$root/data/console/.status.lock"; do
  if [ -e "$file" ]; then
    rm -f -- "$file"
    echo "removed $file"
  fi
done
if [ -d "$root/data/status" ] && rmdir -- "$root/data/status" 2>/dev/null; then
  echo "removed $root/data/status"
fi
