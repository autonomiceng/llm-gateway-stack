#!/usr/bin/env python3
"""Opt in to periodic status publication for one explicit checkout and env file."""

import argparse
import fcntl
import os
import stat
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


def read_pair(fd):
    names = (NAME + '.service', NAME + '.timer')
    result = {}
    for name in names:
        for drop_in in (name + '.d', name.rsplit('.', 1)[1] + '.d'):
            try:
                os.stat(drop_in, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise Unavailable()
        regular(fd, name)
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            continue
        with os.fdopen(handle, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 65536):
                raise Unavailable()
            result[name] = stream.read(65537)
    if result and set(result) != set(names):
        raise Unavailable()
    return result


def existing_pair(unit_dir):
    try:
        with directory(unit_dir, create=False) as fd:
            return read_pair(fd)
    except FileNotFoundError:
        return {}


def manager_check(unit_dir, present, runner, *, loaded=False):
    runner(['systemctl', '--user', 'show', '--property=Version'], timeout=10)
    for name in (NAME + '.service', NAME + '.timer'):
        if not loaded:
            try:
                installed = runner(['systemctl', '--user', 'list-unit-files', name,
                                    '--no-legend', '--no-pager'], timeout=10)
                active = runner(['systemctl', '--user', 'list-units', '--all', name,
                                 '--no-legend', '--no-pager'], timeout=10)
            except Unavailable:
                # An absent unit can make enumeration exit 1; show must establish absence.
                pass
            else:
                if not installed.strip() and not active.strip():
                    continue
        evidence = runner(['systemctl', '--user', 'show', name, '--property=FragmentPath',
                           '--property=DropInPaths', '--property=LoadState'], timeout=10)
        fields = set(evidence.strip().splitlines())
        if not loaded and fields == {'FragmentPath=', 'DropInPaths=', 'LoadState=not-found'}:
            continue
        expected = {'FragmentPath=' + str(unit_dir / name), 'DropInPaths=', 'LoadState=loaded'}
        if not present or fields != expected:
            raise Unavailable()


def check_units(contents, unit_dir, runner):
    actual = existing_pair(unit_dir)
    expected = {name: text.encode('utf-8') for name, text in contents.items()}
    if actual and actual != expected:
        raise Unavailable()
    manager_check(unit_dir, bool(actual), runner)
    return actual


def activate(contents, unit_dir, runner):
    # Check the manager before creating even an absent destination. A system unit
    # with this name must never be shadowed by a new per-user installation.
    check_units(contents, unit_dir, runner)
    with directory(unit_dir, 0o700) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = read_pair(fd)
        expected = {name: text.encode('utf-8') for name, text in contents.items()}
        if actual and actual != expected:
            raise Unavailable()
        if not actual:
            written = []
            try:
                for name, payload in expected.items():
                    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                    written.append(name)
                    with os.fdopen(handle, 'wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.fsync(fd)
            except (OSError, UnicodeError):
                for name in written:
                    os.unlink(name, dir_fd=fd)
                os.fsync(fd)
                raise
        # Retain an exact pair after partial activation; the same selection can retry.
        runner(['systemctl', '--user', 'daemon-reload'], timeout=10)
        manager_check(unit_dir, True, runner, loaded=True)
        runner(['systemctl', '--user', 'enable', '--now', NAME + '.timer'], timeout=10)
        if runner(['systemctl', '--user', 'is-enabled', NAME + '.timer'], timeout=10).strip() != 'enabled':
            raise Unavailable()
        if runner(['systemctl', '--user', 'is-active', NAME + '.timer'], timeout=10).strip() != 'active':
            raise Unavailable()


def selected_paths(root, env_file, *, missing_env=False):
    if Path(env_file).is_symlink() and not Path(env_file).exists():
        raise Unavailable()
    root, env_file = Path(os.path.abspath(root)), Path(os.path.abspath(env_file))
    if not (root / 'compose.yaml').is_file() or not (root / 'scripts/status_observer.py').is_file():
        raise Unavailable()
    if not env_file.is_file() and (not missing_env or env_file.exists() or env_file.is_symlink()):
        raise Unavailable()
    return root, env_file


def check(root, env_file, unit_dir, runner=run):
    unit_dir = Path(os.path.abspath(unit_dir))
    present = existing_pair(unit_dir)
    root, env_file = selected_paths(root, env_file, missing_env=not present)
    check_units(units(root, env_file), unit_dir, runner)


def install(root, env_file, unit_dir, runner=run):
    root, env_file = selected_paths(root, env_file)
    activate(units(root, env_file), Path(os.path.abspath(unit_dir)), runner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--install', action='store_true', help='install or retry this exact timer pair')
    action.add_argument('--check', action='store_true', help='check the selection and user units without writes')
    args = parser.parse_args()
    config = Path(os.environ.get('XDG_CONFIG_HOME', ''))
    if not config.is_absolute():
        config = Path.home() / '.config'
    unit_dir = config / 'systemd/user'
    try:
        if args.check:
            check(args.checkout, args.env_file, unit_dir)
            return 0
        install(args.checkout, args.env_file, unit_dir)
    except (OSError, UnicodeError, Unavailable):
        print(f"status timer {'check' if args.check else 'installation'} failed; preserve units and retry the same selection. "
              'Foreign or partial pairs require inspection; no existing units were overwritten.', file=sys.stderr)
        return 1
    print('Status timer enabled. An active user manager with Docker access is required; '
          'enable lingering separately for observation after logout.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
