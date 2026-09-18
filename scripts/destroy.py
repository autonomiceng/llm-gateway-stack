#!/usr/bin/env python3
"""Deliberately remove an installation's containers and durable volumes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import bootstrap

ROOT = Path(__file__).resolve().parent.parent


def checked(args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Docker command failed; no further deletion attempted')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--include-postgres', action='store_true')
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    settings = {m['key']: bootstrap.unquote(m['value'])
                for m in map(bootstrap.ENV_LINE.match, env_file.read_text().splitlines()) if m}
    project = bootstrap.project_name(settings)
    print(f'This permanently destroys {project} volumes.' +
          (' Postgres data will also be deleted.' if args.include_postgres else ''), flush=True)
    if input('Type the project name to continue: ') != project:
        raise RuntimeError('project name did not match; nothing removed')
    command = ['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), '--project-directory', str(ROOT),
               '--env-file', str(env_file)]
    config = json.loads(checked(command + ['config', '--format', 'json']))
    if config['name'] != project:
        raise RuntimeError('resolved project differs; nothing removed')
    data = Path(next(v['source'] for v in config['services']['postgres']['volumes']
                     if v['target'] == '/var/lib/postgresql')).resolve()
    if data in (ROOT, *ROOT.parents, Path.home()):
        raise RuntimeError('unsafe Postgres data path; nothing removed')
    helper = ['docker', 'run', '--rm', '--network', 'none', '--user', '0',
              '--mount', f'type=bind,src={data},dst=/target', '--entrypoint', 'sh',
              config['services']['postgres']['image'], '-ec']
    if data.exists() and not args.include_postgres:
        try:
            checked(helper + ['entries=$(ls -A /target); test -z "$entries"'])
        except RuntimeError as error:
            raise RuntimeError('Postgres directory is non-empty or unreadable; use --include-postgres to delete it') from error
    volumes = [v['name'] for v in config['volumes'].values()]
    prefix = os.environ.get('LG_VOLUME_PREFIX', settings.get('LG_VOLUME_PREFIX')) or bootstrap.PROJECT
    if set(volumes) != set(bootstrap.volume_names(prefix)):
        raise RuntimeError('unexpected volume names; nothing removed')
    checked(command + ['down', '--remove-orphans'])
    existing = set(checked(['docker', 'volume', 'ls', '--format', '{{.Name}}']).split())
    for volume in volumes:
        if volume in existing:
            checked(['docker', 'volume', 'rm', volume])
    if args.include_postgres and data.exists():
        checked(helper + ['find /target -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'])
    print('Installation destroyed. Backups and .env retained.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f'Refused: {error}', file=sys.stderr)
        sys.exit(1)
