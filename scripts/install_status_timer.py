#!/usr/bin/env python3
"""Opt in to periodic status publication for one explicit checkout and env file."""

import argparse
import os
import sys
from pathlib import Path

from status_io import Unavailable, directory, regular, run

NAME = 'llm-gateway-status'


def quote(value):
    # systemd performs specifier and environment expansion even without a shell.
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Unavailable()
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def units(root, env_file):
    command = ' '.join(quote(part) for part in (
        sys.executable, root / 'scripts/status_observer.py', '--checkout', root,
        '--env-file', env_file))
    service = f'''[Unit]
Description=LLM Gateway public status observation

[Service]
Type=oneshot
ExecStart={command}
TimeoutStartSec=90
UMask=0022
NoNewPrivileges=true
StandardOutput=null
StandardError=journal
'''
    timer = f'''[Unit]
Description=Refresh LLM Gateway public status observations

[Timer]
OnStartupSec=10s
OnUnitInactiveSec=30s
AccuracySec=1s
Unit={NAME}.service

[Install]
WantedBy=timers.target
'''
    return {NAME + '.service': service, NAME + '.timer': timer}


def install(root, env_file, unit_dir, runner=run):
    root, env_file = Path(os.path.abspath(root)), Path(os.path.abspath(env_file))
    if not (root / 'compose.yaml').is_file() or not env_file.is_file():
        raise Unavailable()
    if not (root / 'scripts/status_observer.py').is_file():
        raise Unavailable()
    contents = units(root, env_file)
    # Refuse to overwrite existing units, including a previous selection. Operators
    # disable/remove the old pair explicitly before selecting another installation.
    with directory(unit_dir, 0o700) as fd:
        for name in contents:
            regular(fd, name)
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Unavailable()
        written = []
        try:
            for name, text in contents.items():
                handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
                written.append(name)
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
        except (OSError, UnicodeError):
            for name in written:
                os.unlink(name, dir_fd=fd)
            raise
    runner(['systemctl', '--user', 'daemon-reload'], timeout=10)
    runner(['systemctl', '--user', 'enable', '--now', NAME + '.timer'], timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--install', action='store_true', required=True,
                        help='write user units and enable the timer now')
    args = parser.parse_args()
    config = Path(os.environ.get('XDG_CONFIG_HOME', ''))
    if not config.is_absolute():
        config = Path.home() / '.config'
    unit_dir = config / 'systemd/user'
    try:
        install(args.checkout, args.env_file, unit_dir)
    except (OSError, UnicodeError, Unavailable):
        print('status timer installation failed; disable --now the retained timer and service before inspecting or removing their unit files', file=sys.stderr)
        return 1
    print('Status timer enabled. An active user manager with Docker access is required; '
          'enable lingering separately for observation after logout.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
