#!/usr/bin/env python3
"""Take or restore a whole-stack Checkpoint."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import bootstrap

ROOT = Path(__file__).resolve().parent.parent


def run(argv, timeout=None):
    with subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as error:
            # This group belongs to the child we spawned, including the Compose plugin.
            try:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                # TimeoutExpired includes argv, which can contain credentials.
                raise RuntimeError('command timed out') from None
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def checked(argv, runner=run, *, diagnostics, label='command', timeout=None):
    result = runner(argv, timeout=timeout) if timeout is not None else runner(argv)
    if result.returncode:
        # Output can contain credentials. Labels are supplied by callers, never argv.
        try:
            diagnostics.mkdir(exist_ok=True, mode=0o700)
            os.chmod(diagnostics, 0o700)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            path = diagnostics / f'{stamp}-{label}.log'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(result.stdout + result.stderr)
        except OSError as error:
            raise RuntimeError(f'{label} failed (exit {result.returncode}); diagnostics unavailable') from error
        # Emit only fixed classifications; raw command output remains private.
        reason = next((name for pattern, name in (
            ('unknown flag', 'unsupported_compose_flag'),
            ('is unhealthy', 'container_unhealthy'),
            ('no space left on device', 'filesystem_full'),
            ('dependency failed', 'dependency_failed'),
            ('did not complete successfully', 'dependency_incomplete'),
        ) if pattern in (result.stdout + result.stderr).lower()), 'command_failed')
        raise RuntimeError(f'{label} failed (exit {result.returncode}, {reason}); diagnostics: {path}')
    return result.stdout.strip()


def image_refs(config):
    return {name: service['image'] for name, service in config['services'].items()}


def immutable(ref):
    return re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}', ref) is not None


def image_repository(ref):
    repository = ref.split('@', 1)[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    return repository.removeprefix('docker.io/').removeprefix('index.docker.io/').removeprefix('library/')


def inventory(directory):
    out = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Checkpoint must not contain symlinks')
        if not path.is_file() or path.name == 'manifest.json' and path.parent == directory:
            continue
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        out[path.relative_to(directory).as_posix()] = {
            'size': path.stat().st_size, 'sha256': digest.hexdigest(),
        }
    return out


def manifest(directory, images, env_file, commit, timestamp, fenced, restore_point, timeline_id):
    # Record names only. Never serialize the resolved Compose environment.
    keys = sorted(set(re.findall(r'^([A-Z][A-Z0-9_]*)=', env_file.read_text(), re.M)))
    return {
        'version': 1, 'timestamp': timestamp, 'git_commit': commit,
        'images': images, 'env_keys': keys, 'fenced': fenced,
        'postgres_restore_point': restore_point, 'postgres_timeline_id': timeline_id,
        'clickhouse_database': 'default',
        'artifacts': inventory(directory),
    }


def wait_idle(poller, timeout=300, interval=2, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    quiet = 0
    while clock() < deadline:
        quiet = quiet + 1 if poller() == 0 else 0
        if quiet == 3:
            return
        sleep(interval)
    raise RuntimeError('worker did not become idle before the fence timeout')


def check_empty(data_dir, project, image, runner=run, *, diagnostics, volumes=()):
    running = checked(['docker', 'ps', '-q', '--filter',
                       f'label=com.docker.compose.project={project}'], runner,
                      diagnostics=diagnostics, label='restore-containers')
    if running:
        raise RuntimeError('restore requires every project container stopped')
    if not data_dir.is_dir():
        raise RuntimeError('restore requires a Postgres data directory')
    mounts = [('bind', str(data_dir))]
    names = checked(['docker', 'volume', 'ls', '--format', '{{.Name}}'], runner,
                    diagnostics=diagnostics, label='restore-volumes').split()
    labelled = checked(['docker', 'volume', 'ls', '--filter',
                        f'label=com.docker.compose.project={project}', '--format', '{{.Name}}'], runner,
                       diagnostics=diagnostics, label='restore-project-volumes').split()
    mounts += [('volume', name) for name in sorted(set(labelled) | {
        name for name in names if name.startswith(project + '_') or name in volumes})]
    for kind, source in mounts:
        result = runner(['docker', 'run', '--rm', '--network', 'none', '--user', '0',
                         '--mount', f'type={kind},src={source},dst=/target,readonly',
                         '--entrypoint', 'sh', image, '-ec',
                         'entries=$(ls -A /target); test -z "$entries"'])
        if result.returncode:
            raise RuntimeError(f'restore refused: non-empty or unreadable target {source}')


def retention_plan(backups, keep, archive, segment_size):
    if keep < 1:
        raise ValueError('LG_BACKUP_KEEP must be at least 1')
    complete = sorted(p for p in backups.iterdir()
                      if re.fullmatch(r'\d{8}T\d{12}Z', p.name)
                      and p.is_dir() and not p.is_symlink() and (p / 'manifest.json').is_file())
    if len(complete) < 2:
        return [], []
    retained = complete[-keep:]
    starts = []
    for path in retained:
        doc = json.loads((path / 'postgres/backup_manifest').read_text())
        if not doc['WAL-Ranges']:
            raise RuntimeError('retained base backup has no start WAL')
        for wal_range in doc['WAL-Ranges']:
            if not re.fullmatch(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}', wal_range['Start-LSN']):
                raise RuntimeError('invalid base backup start LSN')
            high, low = (int(part, 16) for part in wal_range['Start-LSN'].split('/'))
            segment = ((high << 32) + low) // segment_size
            per_log = (1 << 32) // segment_size
            starts.append(f'{segment // per_log:08X}{segment % per_log:08X}')
    if not starts:
        raise RuntimeError('retained base backups have no start WAL')
    # pg_archivecleanup ignores timeline IDs; history files must survive PITR.
    boundary = min(starts)
    wal = [name for name in archive
           if re.fullmatch(r'[0-9A-F]{24}(?:\.partial|\.[0-9A-F]{8}\.backup)?', name)
           and name[8:24] < boundary]
    return [p.name for p in complete[:-keep]], sorted(wal)


def prune(stack):
    archive = stack.helper('postgres',
        'find /backup/archive -maxdepth 1 -type f -printf "%f\\n"').splitlines()
    size = int(stack.pg("SELECT pg_size_bytes(current_setting('wal_segment_size'))"))
    old, wal = retention_plan(stack.backups, stack.keep, archive, size)
    # The postgres helper can remove archive files and ClickHouse-owned artifacts.
    for name in old:
        stack.helper('postgres', 'rm -rf -- "$1"', f'/backup/{name}')
    for offset in range(0, len(wal), 256):
        stack.helper('postgres', 'rm -f -- "$@"',
                     *(f'/backup/archive/{name}' for name in wal[offset:offset + 256]))


def write_metrics(success):
    console = ROOT / 'data/console'
    console.mkdir(parents=True, exist_ok=True)
    path = console / 'metrics.txt'
    previous = path.read_text() if path.exists() else ''
    match = re.search(r'^lg_checkpoint_timestamp_seconds ([0-9.]+)$', previous, re.M)
    timestamp = time.time() if success else float(match[1]) if match else 0
    temporary = path.with_suffix('.tmp')
    temporary.write_text(
        '# HELP lg_checkpoint_timestamp_seconds Last successful Checkpoint Unix timestamp.\n'
        '# TYPE lg_checkpoint_timestamp_seconds gauge\n'
        f'lg_checkpoint_timestamp_seconds {timestamp:.0f}\n'
        '# HELP lg_checkpoint_success Whether the latest Checkpoint attempt succeeded.\n'
        '# TYPE lg_checkpoint_success gauge\n'
        f'lg_checkpoint_success {int(success)}\n')
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


# BullMQ ready/active work, including priorities and paused jobs. Ingestion delays
# must drain too; future recurring maintenance jobs remain in the saved AOF.
# ARGV[2] == 'active' counts only in-flight jobs: after the worker has stopped,
# recurring maintenance jobs keep coming due in `delayed` and are not work lost.
QUEUE_LUA = '''
local cursor = '0'
local count = 0
local only_active = ARGV[2] == 'active'
repeat
  local page = redis.call('SCAN', cursor, 'MATCH', 'bull:*', 'COUNT', 1000)
  cursor = page[1]
  for _, key in ipairs(page[2]) do
    local suffix = string.match(key, ':([^:]+)$')
    if only_active then
      if suffix == 'active' then
        count = count + redis.call('LLEN', key)
      end
    elseif suffix == 'wait' or suffix == 'active' or suffix == 'paused' then
      count = count + redis.call('LLEN', key)
    elseif suffix == 'prioritized' then
      count = count + redis.call('ZCARD', key)
    elseif suffix == 'delayed' then
      if string.find(key, 'ingestion') then
        count = count + redis.call('ZCARD', key)
      else
        count = count + redis.call('ZCOUNT', key, '-inf', ARGV[1])
      end
    end
  end
until cursor == '0'
return count
'''


def check_storage(backups, data, allow_same_filesystem=False):
    try:
        bootstrap.check_backup_storage(backups, data, allow_same_filesystem)
    except bootstrap.Refused as error:
        raise RuntimeError(error.detail) from error


class Stack:
    def __init__(self, env_file, runner=run):
        self.runner = runner
        self.env_file = env_file.resolve()
        if not self.env_file.is_file():
            raise RuntimeError('the original .env is required')
        lines, saved = bootstrap.read_env(self.env_file)
        missing = sorted(key for key in bootstrap.MANAGED if not saved.get(key))
        if missing:
            raise RuntimeError('the original .env is missing managed values: ' + ', '.join(missing))
        conflicts = sorted(key for key in bootstrap.MANAGED
                           if key in os.environ and os.environ[key] != saved[key])
        if conflicts:
            raise RuntimeError('shell secrets differ from the original .env: ' + ', '.join(conflicts))
        settings = {m.group('key'): bootstrap.unquote(m.group('value'))
                    for m in map(bootstrap.ENV_LINE.match, lines) if m}
        allow_same_filesystem = bootstrap.backup_policy(settings)
        backup_dir = os.environ.get('LG_BACKUP_DIR', settings.get('LG_BACKUP_DIR', ''))
        if not backup_dir:
            raise RuntimeError('LG_BACKUP_DIR is required')
        self.backups = (ROOT / backup_dir).resolve()
        data_dir = os.environ.get('LG_POSTGRES_DATA_DIR') or settings.get('LG_POSTGRES_DATA_DIR') or './data/postgres'
        check_storage(self.backups, (ROOT / data_dir).resolve(), allow_same_filesystem)
        self.storage_overrides = sorted(key for key in (
            'LG_POSTGRES_DATA_DIR', 'LG_VOLUME_PREFIX', 'LG_BACKUP_DIR', 'COMPOSE_PROJECT_NAME',
            'LG_ALLOW_SAME_FILESYSTEM_BACKUP')
            if key in os.environ and os.environ[key] != settings.get(key))
        # Preserve the same mode files and operator overrides used by bootstrap.
        self.command = ['docker', 'compose', '--project-directory', str(ROOT),
                        '--env-file', str(self.env_file)]
        self.config = json.loads(self.dc('config', '--format', 'json'))
        self.images = image_refs(self.config)
        self.project = self.config['name']
        self.volumes = [v['name'] for v in self.config['volumes'].values()]
        self.prefix = os.environ.get('LG_VOLUME_PREFIX', settings.get('LG_VOLUME_PREFIX')) or bootstrap.PROJECT
        self.keep = int(os.environ.get('LG_BACKUP_KEEP') or settings.get('LG_BACKUP_KEEP') or '7')
        if self.keep < 1:
            raise ValueError('LG_BACKUP_KEEP must be at least 1')
        self.backups = self.mount('postgres', '/backup')
        self.data = self.mount('postgres', '/var/lib/postgresql')
        check_storage(self.backups, self.data, allow_same_filesystem)

    def dc(self, *args, label='compose', timeout=None):
        return checked(self.command + list(args), self.runner,
                       diagnostics=self.backups / '.diagnostics', label=label, timeout=timeout)

    # Langfuse's pinned web shutdown waits 110s and leaves process exit to its supervisor.
    # Require its backend-close marker, and the worker's writer-flush marker, before capture.
    CLEAN_EXIT_REQUIRED = ('caddy', 'litellm', 'valkey')
    WORKER_DONE = 'Shutdown complete, exiting process'
    WEB_DONE = 'Shutdown complete'

    def stop(self, service, timeout):
        started = datetime.now(timezone.utc).isoformat()
        self.dc('stop', '-t', str(timeout), service, label='fence-stop')
        if service in ('langfuse-web', 'langfuse-worker'):
            logs = self.dc('logs', '--since', started, '--no-log-prefix', service, label='fence-worker-log')
            complete = (self.WORKER_DONE in logs if service == 'langfuse-worker' else
                        self.WEB_DONE in logs and 'Prisma connection has been closed.' in logs)
            if not complete:
                raise RuntimeError(f'{service} did not finish its shutdown flush; retry backup')
            return
        if service not in self.CLEAN_EXIT_REQUIRED:
            return
        output = self.dc('ps', '-a', '--format', 'json', service, label='fence-exit')
        containers = (json.loads(output) if output.lstrip().startswith('[')
                      else [json.loads(line) for line in output.splitlines() if line.strip()])
        if not containers:
            raise RuntimeError(f'service {service} did not stop cleanly (exit unknown)')
        for container in containers:
            code = container.get('ExitCode')
            if container.get('State') != 'exited' or code != 0:
                raise RuntimeError(f'service {service} did not stop cleanly (exit {code})')

    def mount(self, service, target):
        return Path(next(v['source'] for v in self.config['services'][service]['volumes']
                         if v['target'] == target))

    def capture_images(self):
        """Resolve locally verifiable immutable references before any fencing."""
        self.image_ids = {}
        refs = {}
        for service, ref in image_refs(self.config).items():
            output = checked(['docker', 'image', 'inspect', ref, '--format',
                              '{{.Id}} {{json .RepoDigests}}'], self.runner,
                             diagnostics=self.backups / '.diagnostics', label='capture-image')
            image_id, digests = output.split(' ', 1)
            candidates = sorted((value for value in json.loads(digests) or [] if immutable(value)),
                                key=lambda value: (image_repository(value) != image_repository(ref), value))
            if not immutable(ref) and not candidates:
                raise RuntimeError(f'{service}: image has no verifiable immutable reference; publish and pull it before backup')
            refs[service] = ref if immutable(ref) else candidates[0]
            resolved_id = checked(['docker', 'image', 'inspect', refs[service], '--format', '{{.Id}}'],
                                  self.runner, diagnostics=self.backups / '.diagnostics', label='capture-identity')
            if resolved_id != image_id:
                raise RuntimeError(f'immutable image content differs from configured image: {service}')
            self.image_ids[service] = image_id
        self.images = refs
        if any(not immutable(ref) for ref in image_refs(self.config).values()):
            print('Image custody is external: retain recorded references in a registry or a tested off-host image archive; publication was not checked.', file=sys.stderr, flush=True)

    def attest_runtime(self):
        """Refuse checkout/runtime drift before fencing or creating capture artifacts."""
        self.capture_images()
        diagnostics = self.backups / '.diagnostics'
        ids = checked(['docker', 'ps', '-aq', '--filter',
                       f'label=com.docker.compose.project={self.project}'], self.runner,
                      diagnostics=diagnostics, label='capture-containers').split()
        if not ids:
            raise RuntimeError('backup requires project containers')
        containers = json.loads(checked(['docker', 'inspect', *ids], self.runner,
                                        diagnostics=diagnostics, label='capture-inspect'))
        for container in containers:
            labels = container['Config']['Labels']
            service = labels.get('com.docker.compose.service')
            if (service not in self.config['services'] or
                    labels.get('com.docker.compose.oneoff', '').lower() == 'true'):
                raise RuntimeError('unexpected project container; stop and remove it before backup')
            expected = self.config['services'][service]
            if container['Config']['Image'] != expected['image']:
                raise RuntimeError(f'running image differs from resolved Compose: {service}')
            if container['Image'] != self.image_ids[service]:
                raise RuntimeError(f'running image content differs from resolved Compose: {service}')
            mounts = {}
            for mount in expected.get('volumes', []):
                if mount['type'] not in ('bind', 'volume'):
                    continue
                source = mount['source']
                if mount['type'] == 'volume':
                    source = self.config['volumes'][source]['name']
                mounts[mount['target']] = (mount['type'], source, not mount.get('read_only', False))
            actual = {m['Destination']: (m['Type'], m['Name'] if m['Type'] == 'volume'
                                        else m['Source'], m['RW'])
                      for m in container['Mounts'] if m['Type'] in ('bind', 'volume')}
            if actual != mounts:
                raise RuntimeError(f'running persistent mounts differ from resolved Compose: {service}')

    def pg(self, sql):
        return self.dc('exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-d', 'postgres',
                       '-At', '-v', 'ON_ERROR_STOP=1', '-c', sql)

    def ch(self, sql):
        return self.dc('exec', '-T', 'clickhouse', 'sh', '-ec',
                       'exec clickhouse-client --user "$CLICKHOUSE_USER" '
                       '--password "$CLICKHOUSE_PASSWORD" --query "$1"', 'sh', sql)

    def helper(self, service, script, *args, mounts=(), user='0', env=()):
        return self.dc('run', '--pull', 'never', '--rm', '--no-deps', '-T', '--user', user,
                       *env, *mounts, '--entrypoint', 'sh', service, '-ec', script, 'sh', *args)

    def queue_depth(self):
        self.dc('exec', '-T', 'langfuse-worker', 'wget', '-qO-',
                'http://127.0.0.1:3030/api/health')
        return self.queue_count()

    def queue_count(self, mode='all'):
        result = self.dc('exec', '-T', 'valkey', 'sh', '-ec',
                         'export VALKEYCLI_AUTH="$VALKEY_PASSWORD"; '
                         'exec valkey-cli --raw EVAL "$1" 0 "$2" "$3"',
                         'sh', QUEUE_LUA, str(int(time.time() * 1000) * 4096 + 4095), mode)
        return int(result)

    def objects(self, direction, path):
        local = self.backups / Path(path).relative_to('/backup')
        sidecar = local.parent / 'objects.meta.json'

        def aws(script, *args):
            # Dropped capabilities mean root cannot read operator-owned 0700 files.
            return self.helper('rustfs-init',
                'aws configure set default.s3.addressing_style path; ' + script,
                *args, mounts=('-v', f'{self.backups}:/backup'),
                user=f'{os.getuid()}:{os.getgid()}', env=('-e', 'HOME=/tmp'))

        if direction == 'backup':
            aws('aws --endpoint-url http://rustfs:9000 s3 sync s3://langfuse "$1" --only-show-errors', path)
            # A concurrent unfenced upload must not add a sidecar entry for an object
            # that was absent from the sync. A vanished object makes head-object fail.
            keys = sorted(p.relative_to(local).as_posix()
                          for p in (local / 'media').rglob('*') if p.is_file())
            parts = local.parent / 'objects.meta.parts'
            parts.mkdir(mode=0o700)
            (parts / 'keys').write_bytes(b''.join(
                f'{index}\0{key}\0'.encode() for index, key in enumerate(keys)))
            aws('''xargs -0 -r -n 2 -P 8 sh -ec '
                aws --endpoint-url http://rustfs:9000 s3api head-object \
                  --bucket langfuse --key "$2" --output json > "$0/$1.json"
                ' "$1" < "$1/keys"''', str(Path(path).parent / parts.name))
            metadata = {}
            for index, key in enumerate(keys):
                head = json.loads((parts / f'{index}.json').read_text())
                metadata[key] = {field: head[field] for field in
                    ('ContentType', 'ContentDisposition', 'ContentEncoding') if head.get(field)}
                if 'ContentType' not in metadata[key]:
                    metadata[key]['ContentType'] = 'application/octet-stream'
            sidecar.write_text(json.dumps(metadata, indent=2) + '\n')
            shutil.rmtree(parts)
        else:
            metadata = json.loads(sidecar.read_text())
            media = {p.relative_to(local).as_posix() for p in (local / 'media').rglob('*') if p.is_file()}
            if media != set(metadata):
                raise RuntimeError('media objects and metadata sidecar differ')
            aws('aws --endpoint-url http://rustfs:9000 s3 sync "$1" s3://langfuse '
                '--exclude "media/*" --content-type application/json --only-show-errors', path)
            commands = []
            for key, head in metadata.items():
                args = ['aws', '--endpoint-url', 'http://rustfs:9000', 's3', 'cp',
                        str(Path(path) / key), 's3://langfuse/' + key, '--only-show-errors',
                        '--content-type', head['ContentType']]
                for field, flag in (('ContentDisposition', '--content-disposition'),
                                    ('ContentEncoding', '--content-encoding')):
                    if field in head:
                        args += [flag, head[field]]
                commands.append(shlex.join(args))
            # Bound command size for buckets with many media objects.
            for offset in range(0, len(commands), 100):
                aws('\n'.join(commands[offset:offset + 100]))


def backup(stack, no_fence, timeout, stop_timeout=120):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = stack.backups / stamp
    point = 'checkpoint_' + stamp
    # Require running services so the failure handler never starts an idle installation.
    required = {'caddy', 'litellm', 'langfuse-web', 'langfuse-worker',
                'postgres', 'clickhouse', 'rustfs', 'valkey'}
    running = set(stack.dc('ps', '--status', 'running', '--services').split())
    if not required <= running:
        raise RuntimeError('backup requires the complete stack running')
    stack.attest_runtime()
    if stack.pg("SHOW archive_mode") != 'on':
        raise RuntimeError('Postgres must be restarted with WAL archiving enabled')
    if stack.pg("SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')") != '0':
        raise RuntimeError('custom PostgreSQL tablespaces are unsupported')
    dest.mkdir(mode=0o711)
    os.chmod(dest, 0o711)
    (dest / 'objects').mkdir()
    (dest / 'clickhouse').mkdir()
    stack.dc('exec', '-T', '--user', '0', 'postgres', 'sh', '-ec',
             'chown "101:$2" "$1"; chmod 750 "$1"',
             'sh', f'/backup/{stamp}/clickhouse', str(os.getgid()))
    stopped = []
    paused = False
    capture_error = None
    handlers = {}
    try:
        if not no_fence:
            for service in ('caddy', 'litellm', 'langfuse-web'):
                stopped.append(service)
                stack.stop(service, stop_timeout)
            wait_idle(stack.queue_depth, timeout)
            stopped.append('langfuse-worker')
            stack.stop('langfuse-worker', stop_timeout)
            if stack.queue_count('active'):
                raise RuntimeError('worker still had jobs in flight after stopping; retry backup')
            stopped.append('valkey')
            stack.stop('valkey', stop_timeout)
        else:
            # Freeze AOF rotation for the copy. The unfenced stores can diverge.
            paused = True
            stack.dc('pause', 'valkey')
        stack.helper('valkey', 'umask 077; tar -C /data -cf "$1" appendonlydir',
                     f'/backup/{stamp}/valkey.tar', mounts=('-v', f'{stack.backups}:/backup'))
        if paused:
            stack.dc('unpause', 'valkey', label='fence-resume')
            paused = False
        stack.ch(f"BACKUP DATABASE default TO Disk('backups', '{stamp}/clickhouse/backup.zip')")
        stack.dc('exec', '-T', '--user', '0', 'postgres', 'sh', '-ec',
                 'chown "101:$2" "$1"; chmod 640 "$1"',
                 'sh', f'/backup/{stamp}/clickhouse/backup.zip', str(os.getgid()))
        stack.objects('backup', f'/backup/{stamp}/objects')
        first = stack.pg('SELECT pg_walfile_name(pg_current_wal_lsn())')
        stack.dc('exec', '-T', '--user', '0', 'postgres', 'pg_basebackup', '-U', 'postgres',
                 '-D', f'/backup/{stamp}/postgres', '-Ft', '-X', 'stream', '--checkpoint=fast')
        stack.pg(f"SELECT pg_create_restore_point('{point}')")
        last = stack.pg('SELECT pg_walfile_name(pg_switch_wal())')
        # Wait for, and retain, every segment from the base backup through the target.
        stack.dc('exec', '-T', '--user', '0', 'postgres', 'sh', '-ec', '''
            umask 077
            dest=$1; first=$2; last=$3
            mkdir "$dest/wal"
            n=0
            while [ ! -f "/backup/archive/$last" ]; do
              n=$((n + 1)); [ "$n" -lt 120 ] || exit 1; sleep 1
            done
            for file in /backup/archive/*; do
              name=${file##*/}
              case "$name" in
                *.history) cp "$file" "$dest/wal/";;
                ????????????????????????)
                  if [ "$name" = "$first" ] || { [ "$name" \\> "$first" ] && [ "$name" \\< "$last" ]; } || [ "$name" = "$last" ]; then
                    cp "$file" "$dest/wal/"
                  fi;;
              esac
            done
            test -f "$dest/wal/$first"
            chown -R "$4:$5" "$dest"
            chmod -R u+rwX,go-rwx "$dest"
            chmod 711 "$dest"
            chown -R "101:$5" "$dest/clickhouse"
            chmod 750 "$dest/clickhouse"
            chmod 640 "$dest/clickhouse/backup.zip"
            sync -f "$dest"
        ''', 'sh', f'/backup/{stamp}', first, last, str(os.getuid()), str(os.getgid()))
        doc = manifest(dest, stack.images, stack.env_file,
                       checked(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                               diagnostics=stack.backups / '.diagnostics', label='git-revision'),
                       datetime.now(timezone.utc).isoformat(), not no_fence, point, int(first[:8], 16))
        with (dest / 'manifest.json').open('x') as handle:
            json.dump(doc, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        print(f'Checkpoint: {dest}', flush=True)
    except BaseException as error:
        capture_error = error
        raise
    finally:
        try:
            try:
                for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    handlers[sig] = signal.signal(sig, signal.SIG_IGN)
            finally:
                resume(stack, stopped, paused, stop_timeout + 10 if capture_error else 0)
        except BaseException as resume_error:
            if capture_error is not None:
                raise RuntimeError(f'capture failed: {capture_error}; '
                                   f'service resumption also failed: {resume_error}') from capture_error
            raise
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def resume(stack, stopped, paused, settle=0):
    if paused:
        stack.dc('unpause', 'valkey', label='fence-resume', timeout=120)
    if stopped:
        try:
            stack.dc('up', '-d', '--no-deps', '--no-recreate', *reversed(stopped),
                     label='fence-resume', timeout=120)
        except RuntimeError:
            pass  # The health loop retries a transient or partly completed start.
        wait_healthy(stack, stopped, settle=settle)
    health(stack, timeout=120)


def wait_healthy(stack, services, timeout=300, settle=0):
    settle_until = time.monotonic() + settle
    deadline = settle_until + timeout
    last_error = None
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise RuntimeError('resumed services did not become healthy') from last_error
        return min(120, seconds)

    while True:
        try:
            output = stack.dc('ps', '--format', 'json', *services, label='fence-health',
                              timeout=remaining())
            containers = (json.loads(output) if output.lstrip().startswith('[')
                          else [json.loads(line) for line in output.splitlines() if line.strip()])
            running = {row.get('Service') for row in containers if row.get('State') == 'running'}
            if (len(containers) == len(services) and running == set(services)
                    and all(row.get('Health') == 'healthy' for row in containers)
                    and (not settle or time.monotonic() >= settle_until)):
                return
            # An interrupted stop can finish after the initial start request.
            if running != set(services):
                stack.dc('up', '-d', '--no-deps', '--no-recreate', *reversed(services),
                         label='fence-resume', timeout=remaining())
        except (RuntimeError, ValueError) as error:
            last_error = error
        if time.monotonic() >= deadline:
            raise RuntimeError('resumed services did not become healthy') from last_error
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def verify_checkpoint(source, images):
    doc = json.loads((source / 'manifest.json').read_text())
    if (doc['version'] != 1 or doc['images'] != images or not all(map(immutable, images.values()))
            or doc['clickhouse_database'] != 'default'):
        raise RuntimeError('restore requires the same immutable image references and Checkpoint format')
    if not re.fullmatch(r'checkpoint_\d{8}T\d{12}Z', doc['postgres_restore_point']):
        raise RuntimeError('invalid restore point')
    timeline = doc.get('postgres_timeline_id')
    if type(timeline) is not int or not 1 <= timeline <= 0xFFFFFFFF:
        raise RuntimeError('Checkpoint requires a valid Postgres timeline id')
    required = {'postgres/base.tar', 'postgres/pg_wal.tar', 'postgres/backup_manifest',
                'clickhouse/backup.zip', 'valkey.tar', 'objects.meta.json'}
    if not required <= doc['artifacts'].keys() or not (source / 'objects').is_dir():
        raise RuntimeError('incomplete Checkpoint')
    if not any(re.fullmatch(r'wal/[0-9A-F]{24}', name) for name in doc['artifacts']):
        raise RuntimeError('Checkpoint has no archived WAL')
    if doc['artifacts'] != inventory(source):
        raise RuntimeError('Checkpoint checksum or size mismatch')
    metadata = json.loads((source / 'objects.meta.json').read_text())
    media = {p.relative_to(source / 'objects').as_posix()
             for p in (source / 'objects/media').rglob('*') if p.is_file()}
    if (not isinstance(metadata, dict) or media != set(metadata)
            or any(not isinstance(head, dict) or not isinstance(head.get('ContentType'), str)
                   or not head['ContentType'] for head in metadata.values())):
        raise RuntimeError('media objects and metadata sidecar differ or lack content type')
    # Only our regular files/directories may be unpacked into empty target storage.
    for name in ('postgres/base.tar', 'postgres/pg_wal.tar', 'valkey.tar'):
        with tarfile.open(source / name) as archive:
            for member in archive:
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError('unsupported archive member (including tablespace links)')
    return doc


def health(stack, timeout=None):
    caddy = stack.config['services']['caddy']
    settings = dict(caddy['environment'])
    for port in caddy['ports']:
        if port['target'] in (80, 443):
            settings['LG_HTTP_PORT' if port['target'] == 80 else 'LG_HTTPS_PORT'] = str(port['published'])
            settings['LG_BIND_HOST'] = port.get('host_ip', '127.0.0.1')
    runner = stack.runner if timeout is None else lambda argv: stack.runner(argv, timeout=timeout)
    bootstrap.probe_gateway(settings, stack.command, runner)


def restore(stack, source, allow_unfenced=False):
    source = source.resolve()
    doc = verify_checkpoint(source, stack.images)
    if doc.get('fenced') is not True and not allow_unfenced:
        raise RuntimeError('restore refuses an unfenced Checkpoint without --allow-unfenced')
    for service, ref in stack.images.items():
        try:
            checked(['docker', 'image', 'inspect', ref, '--format', '{{.Id}}'], stack.runner,
                    diagnostics=stack.backups / '.diagnostics', label='restore-image')
        except RuntimeError as error:
            raise RuntimeError(f'{service}: pull the recorded Checkpoint image before restore; {error}') from error
    saved_names = {m['key'] for m in map(bootstrap.ENV_LINE.match, stack.env_file.read_text().splitlines()) if m}
    missing_names = sorted(set(doc.get('env_keys', [])) - saved_names)
    if missing_names:
        print('Checkpoint settings absent from target .env: ' + ', '.join(missing_names), file=sys.stderr)
    bootstrap.ensure_network(stack.runner, stack.config['networks']['platform']['name'])
    if not stack.data.exists():
        stack.data.mkdir(parents=True, mode=0o755)
        os.chmod(stack.data, 0o755)
    bootstrap.write_versions(ROOT, ROOT / 'compose.yaml', stack.images)
    check_empty(stack.data, stack.project, stack.images['postgres'], stack.runner,
                diagnostics=stack.backups / '.diagnostics', volumes=stack.volumes)
    bootstrap.ensure_volumes(stack.runner, stack.prefix)
    # A recovered incarnation must never publish into the source cluster's archive.
    stack.helper('postgres', 'if [ -d /backup/archive ]; then entries=$(ls -A /backup/archive); test -z "$entries"; fi')
    stamp = doc['postgres_restore_point'].removeprefix('checkpoint_')
    destination = stack.backups / stamp
    if source != destination.resolve():
        pending = destination.with_name(destination.name + '.restoring')
        shutil.copytree(source, pending)
        if inventory(pending) != doc['artifacts']:
            raise RuntimeError('restore copy checksum or size mismatch')
        for path in pending.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        directories = [path for path in pending.rglob('*') if path.is_dir()]
        for directory in [*reversed(directories), pending]:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        pending.rename(destination)
        fd = os.open(stack.backups, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    elif inventory(destination) != doc['artifacts']:
        raise RuntimeError('restore copy checksum or size mismatch')
    os.chmod(destination, 0o711)
    target = f'/backup/{stamp}'
    stack.helper('postgres', '''
        chown -R "101:$2" "$1/clickhouse"
        chmod 750 "$1/clickhouse"
        chmod 640 "$1/clickhouse/backup.zip"
    ''', target, str(os.getgid()))
    stack.helper('postgres', '''
        dest=/var/lib/postgresql/18/docker
        mkdir -p "$dest"
        tar -xf "$1/postgres/base.tar" -C "$dest"
        tar -xf "$1/postgres/pg_wal.tar" -C "$dest/pg_wal"
        cp "$1/postgres/backup_manifest" "$dest/backup_manifest"
        pg_verifybackup "$dest"
        rm -rf "$dest/checkpoint-wal"
        mkdir "$dest/checkpoint-wal"
        cp "$1"/wal/* "$dest/checkpoint-wal/"
        printf "\\nrestore_command = 'cp %s/checkpoint-wal/%%f %%p'\\nrecovery_target_name = '%s'\\nrecovery_target_timeline = '%s'\\nrecovery_target_action = 'promote'\\n" "$dest" "$2" "$3" >> "$dest/postgresql.auto.conf"
        touch "$dest/recovery.signal"
        chown -R postgres:postgres /var/lib/postgresql
        chmod 700 "$dest"
    ''', target, doc['postgres_restore_point'], str(doc['postgres_timeline_id']))
    stack.helper('valkey', 'tar -xf "$1/valkey.tar" -C /data; chown -R valkey:valkey /data',
                 target, mounts=('-v', f'{stack.backups}:/backup:ro'))
    # Only storage runs until every artifact has been restored.
    stack.dc('up', '-d', '--no-deps', '--wait', '--wait-timeout', '180',
             'postgres', 'clickhouse', 'rustfs', 'valkey')
    deadline = time.monotonic() + 300
    while stack.pg('SELECT pg_is_in_recovery()') != 'f':
        if time.monotonic() >= deadline:
            raise RuntimeError('Postgres has not reached the Checkpoint restore point')
        time.sleep(2)
    # RESET must be separate statements: ALTER SYSTEM cannot run in a transaction.
    for setting in ('restore_command', 'recovery_target', 'recovery_target_name',
                    'recovery_target_time', 'recovery_target_xid', 'recovery_target_lsn',
                    'recovery_target_inclusive', 'recovery_target_timeline', 'recovery_target_action'):
        stack.pg(f'ALTER SYSTEM RESET {setting}')
    stack.pg('SELECT pg_reload_conf()')
    stack.helper('postgres', 'rm -rf /var/lib/postgresql/18/docker/checkpoint-wal')
    stack.ch(f"RESTORE DATABASE default FROM Disk('backups', '{stamp}/clickhouse/backup.zip')")
    stack.dc('run', '--rm', '--no-deps', '-T', 'rustfs-init')
    stack.objects('restore', f'{target}/objects')
    stack.dc('up', '-d', '--wait', '--wait-timeout', '300')
    health(stack)
    print('Restore complete; gateway and Langfuse health probes passed. '
          'Verify historical encrypted values using the original secrets; Checkpoint v1 cannot attest secret identity.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup_parser = commands.add_parser('backup')
    backup_parser.add_argument('--no-fence', action='store_true')
    backup_parser.add_argument('--fence-timeout', type=int, default=300)
    backup_parser.add_argument('--stop-timeout', type=int, default=120)
    restore_parser = commands.add_parser('restore')
    restore_parser.add_argument('checkpoint', type=Path)
    restore_parser.add_argument('--allow-unfenced', action='store_true')
    for command in (backup_parser, restore_parser):
        command.add_argument('--env-file', type=Path, default=ROOT / '.env')
    args = parser.parse_args()
    if args.command == 'backup' and not args.no_fence and args.stop_timeout < 120:
        parser.error('--stop-timeout must be at least 120 for a fenced backup')
    os.umask(0o077)
    env_file = args.env_file.resolve()
    with ExitStack() as locks:
        lock = locks.enter_context(env_file.with_name(env_file.name + '.lock').open('w'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'backup':
            # Metrics belong to the checkout, even when different env files are used.
            # Serialize attempts before marking preflight failure; contenders leave
            # the active attempt's status untouched.
            console = ROOT / 'data/console'
            console.mkdir(parents=True, exist_ok=True)
            status_lock = locks.enter_context((console / '.checkpoint.lock').open('w'))
            fcntl.flock(status_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            stack = Stack(env_file)
        except (Exception, KeyboardInterrupt):
            if args.command == 'backup':
                write_metrics(False)
            raise
        try:
            repository_lock = locks.enter_context((stack.backups / '.checkpoint.lock').open('w'))
            fcntl.flock(repository_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A refused concurrent invocation must not alter the active capture's status.
            raise
        except OSError:
            if args.command == 'backup':
                write_metrics(False)
            raise
        if args.command == 'restore':
            if stack.storage_overrides:
                print('Restore storage uses shell overrides: ' + ', '.join(stack.storage_overrides) +
                      '. Save these settings in the target .env or retain them for later Compose commands.',
                      file=sys.stderr)
            restore(stack, args.checkpoint, args.allow_unfenced)
        else:
            write_metrics(False)
            backup(stack, args.no_fence, args.fence_timeout, args.stop_timeout)
            prune(stack)
            write_metrics(True)


def interrupted(signum, frame):
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    raise RuntimeError('interrupted; resuming fenced services')


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        main()
    except (bootstrap.Refused, RuntimeError, OSError, ValueError, KeyError, StopIteration, KeyboardInterrupt) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
