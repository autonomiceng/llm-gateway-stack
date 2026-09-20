#!/usr/bin/env python3
"""Publish the version 1 public status allowlist from bounded host observations."""

import argparse
import fcntl
import ipaddress
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from status_io import Unavailable, directory, now, publish, read_json, read_task, regular, run
from status_probes import PATTERNS, configured_image, probe

SERVICES = tuple(PATTERNS)
TASKS = ('bootstrap', 'rustfs-init')
TTL = 120
LIMIT = 1024 * 1024


def timestamp(value, at):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        ceiling = datetime.fromisoformat(at.replace('Z', '+00:00'))
        if parsed.year < 1970 or (parsed - ceiling).total_seconds() > 5:
            return None
        return parsed.isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def empty(component):
    result = {'id': component, 'kind': 'task' if component in TASKS else 'service',
              'configured': None, 'state': 'unknown', 'observedAt': None,
              'validForSeconds': TTL}
    if component in TASKS:
        result['lastExecutionAt'] = None
    return result


def environment():
    # Selection belongs to the explicit env file, not an interactive shell or user
    # manager's inherited stack overrides. Docker connection settings remain usable.
    allowed = {'PATH', 'HOME', 'USER', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR', 'SSH_AUTH_SOCK'}
    return {key: value for key, value in os.environ.items()
            if key in allowed or key.startswith('DOCKER_')}


def configuration(root, env_file, runner):
    text = runner(['docker', 'compose', '--project-directory', str(root),
                   '--env-file', str(env_file), 'config', '--format', 'json'],
                  timeout=10, limit=LIMIT, cwd=root, env=environment())
    doc = read_json(text, LIMIT)
    if (not isinstance(doc, dict) or not isinstance(doc.get('services'), dict)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', doc.get('name', ''))
            or not all(isinstance(value, dict) for value in doc['services'].values())):
        raise Unavailable()
    return doc


def inventory(project, runner):
    text = runner(['docker', 'ps', '--all', '--no-trunc', '--filter',
                   'label=com.docker.compose.project=' + project,
                   '--format', '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Label "com.docker.compose.oneoff"}}'],
                  timeout=10, limit=65536)
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3 or not re.fullmatch(r'[0-9a-f]{64}', fields[0]):
            raise Unavailable()
        container, service, oneoff = fields
        if oneoff.lower() == 'true':
            continue
        if oneoff.lower() != 'false':
            raise Unavailable()
        result.setdefault(service, []).append(container)
    return result


def observe_service(component, candidates, config, inventory_at, runner, clock):
    row = empty(component)
    service = config['services'].get(component)
    if service is None:
        # Omission from a Compose file does not prove an intentional opt-out.
        return row
    row['configured'] = True
    if component in SERVICES:
        row.update(configured_image(component, service.get('image')))
    if candidates is None:
        return row
    if not candidates:
        if component not in TASKS:
            row.update(state='absent', observedAt=inventory_at)
        return row
    if len(candidates) != 1:
        return row  # Replica aggregation is outside this single-host contract.
    container = candidates[0]
    at = clock()  # Conservatively date the whole bounded inspection/probe transaction.
    try:
        docs = read_json(runner(['docker', 'inspect', container], timeout=4, limit=LIMIT), LIMIT)
        if not isinstance(docs, list) or len(docs) != 1:
            raise Unavailable()
        doc = docs[0]
        labels = doc['Config']['Labels']
        if (doc['Id'] != container or labels['com.docker.compose.project'] != config['name']
                or labels['com.docker.compose.service'] != component
                or str(labels.get('com.docker.compose.oneoff', '')).lower() != 'false'):
            raise Unavailable()
        state = doc['State']
        if not isinstance(state, dict):
            raise Unavailable()
        if component in TASKS:
            started = timestamp(state.get('StartedAt'), at)
            if not started:
                return row
            row.update(observedAt=at, lastExecutionAt=started)
            if state.get('Status') == 'running':
                row['state'] = 'starting'
            elif (state.get('Status') == 'exited' and timestamp(state.get('FinishedAt'), at)
                  and datetime.fromisoformat(timestamp(state['FinishedAt'], at)) >= datetime.fromisoformat(started)
                  and type(state.get('ExitCode')) is int):
                row['state'] = 'healthy' if state['ExitCode'] == 0 else 'unavailable'
            return row
        if re.fullmatch(r'sha256:[0-9a-f]{64}', str(doc.get('Image', ''))):
            row['observedImageId'] = doc['Image']
        row['observedAt'] = at
        if state.get('Status') in ('exited', 'dead'):
            row['state'] = 'unavailable'
        elif state.get('Status') == 'restarting':
            row['state'] = 'starting'
        elif state.get('Status') == 'running' and not state.get('Paused'):
            network = config.get('networks', {}).get('default', {}).get('name')
            ip = doc.get('NetworkSettings', {}).get('Networks', {}).get(network, {}).get('IPAddress')
            if ip:
                ipaddress.ip_address(ip)
            try:
                row['state'], release = probe(component, container, ip, runner)
                if release:
                    row['observedVersion'] = release
            except Unavailable:
                row['state'] = 'unavailable'
        return row
    except (Unavailable, KeyError, TypeError, ValueError, AttributeError):
        # Malformed/failed inspection establishes neither absence nor readiness.
        row.pop('observedImageId', None)
        row.update(state='unknown', observedAt=None)
        return row


def collect(root, env_file, runner=run, clock=now):
    rows = {name: empty(name) for name in (*SERVICES, *TASKS)}
    configured_at = clock()
    telemetry = 'unknown'
    try:
        config = configuration(root, env_file, runner)
    except (Unavailable, TypeError, ValueError):
        configured_at = None
    else:
        inventory_at = clock()
        try:
            resources = inventory(config['name'], runner) or None
        except Unavailable:
            resources = None
        names = (*SERVICES, 'rustfs-init')
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(observe_service, name,
                                        resources.get(name, []) if resources is not None else None,
                                        config, inventory_at, runner, clock) for name in names}
            for name, future in futures.items():
                rows[name] = future.result()
        try:
            record = read_task(root, env_file)
            at = clock()
            started = timestamp(record.get('lastExecutionAt'), at)
            # Interrupted bootstrap records stay unknown, even if their PID was reused.
            if started and record.get('state') in ('healthy', 'unavailable', 'unknown'):
                rows['bootstrap'].update(configured=True, state=record['state'],
                                         observedAt=at, lastExecutionAt=started)
        except (OSError, Unavailable):
            pass
    # Telemetry collection belongs to another stack; exporters alone do not prove it.
    return {'schemaVersion': 1, 'stack': 'gateway', 'generatedAt': clock(),
            'configurationObservedAt': configured_at, 'configurationValidForSeconds': TTL,
            'telemetry': telemetry, 'components': list(rows.values())}


def observe(root, env_file, runner=run, clock=now):
    with directory(root / 'data' / 'console') as fd:
        regular(fd, '.status.lock')
        lock = os.open('.status.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            document = collect(root, env_file, runner, clock)
            if len(document['components']) > 32:
                raise Unavailable()
            publish(fd, 'status.json', document)
        finally:
            os.close(lock)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    args = parser.parse_args()
    root = Path(os.path.abspath(args.checkout))
    env_file = Path(os.path.abspath(args.env_file))
    if not (root / 'compose.yaml').is_file() or not env_file.is_file():
        print('status observer: checkout or env file unavailable', file=sys.stderr)
        return 1
    try:
        observe(root, env_file)
        return 0
    except (OSError, Unavailable):
        print('status observer: observation or publication failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
