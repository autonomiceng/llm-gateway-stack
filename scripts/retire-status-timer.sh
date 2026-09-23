#!/bin/sh
# Retire the version 1 status timer and its records (Status v2 upgrade step). Safe to rerun.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
name=llm-gateway-status
if [ -e "$units/$name.timer" ] || [ -e "$units/$name.service" ]; then
  systemctl --user disable --now "$name.timer" "$name.service"
  rm -f -- "$units/$name.timer" "$units/$name.service"
  systemctl --user daemon-reload
  echo "disabled and removed $name.timer and $name.service"
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
