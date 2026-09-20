#!/usr/bin/env python3
"""Destructive only to a new disposable drill project and its temporary directory."""
import base64
import http.cookiejar
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent.parent
project = os.environ.get('SMOKE_PROJECT', 'llm-gateway-drill')
port = os.environ.get('SMOKE_HTTP_PORT', '18090')
https_port = os.environ.get('SMOKE_HTTPS_PORT', '18453')
if not re.fullmatch(r'llm-gateway-drill(?:-[a-z0-9-]+)?', project):
    sys.exit('SMOKE_PROJECT must be llm-gateway-drill or llm-gateway-drill-<suffix>')
# Bootstrap and checkpoint metrics write checkout-wide data/console.
# A separate Compose project cannot isolate those files from an installed checkout.
for name in ('.env', 'data', 'compose.override.yaml', 'compose.override.yml',
             'docker-compose.override.yaml', 'docker-compose.override.yml'):
    if (root / name).exists() or (root / name).is_symlink():
        sys.exit('drill requires a clean disposable checkout without .env, data or Compose overrides')
backup_root = Path(os.environ.get('SMOKE_BACKUP_ROOT', '/tmp')).resolve()
if not backup_root.is_dir():
    sys.exit('SMOKE_BACKUP_ROOT must name an existing directory on a separate mounted filesystem')
data_parent = root / '.scratch' if (root / '.scratch').exists() else root
if backup_root.stat().st_dev == data_parent.stat().st_dev:
    sys.exit('set SMOKE_BACKUP_ROOT to a mounted filesystem separate from checkout .scratch')
# Ambient Compose overrides could attach production volumes or publish other ports.
for key in ('COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES'):
    os.environ.pop(key, None)

# Compose gives exported settings precedence over --env-file. Keep the drill private.
sys.path.insert(0, str(root / 'scripts'))
import bootstrap
import checkpoint
for key in list(os.environ):
    if key.startswith(('LG_', 'LANGFUSE_', 'RUSTFS_', 'CLICKHOUSE_', 'VALKEY_',
                       'POSTGRES_', 'LITELLM_')) or key in bootstrap.MANAGED:
        os.environ.pop(key)
os.environ['COMPOSE_PROJECT_NAME'] = project
os.environ['COMPOSE_FILE'] = str(root / 'compose.yaml')


os.umask(0o077)
(root / '.scratch').mkdir(exist_ok=True)
work = Path(tempfile.mkdtemp(prefix='llm-gateway-drill-', dir=root / '.scratch'))
backup_work = Path(tempfile.mkdtemp(prefix='llm-gateway-drill-', dir=backup_root))
network = project + '-platform'
network_created = False


def run(argv, label='drill-command'):
    if label in ('drill-backup', 'drill-restore'):
        # These entrypoints already sanitize errors; preserve their actionable refusal.
        result = subprocess.run(argv, cwd=root, text=True, stdout=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(f'{label} failed (exit {result.returncode}); see the checkpoint refusal above')
        return result.stdout.strip()
    return checkpoint.checked(argv,
        lambda args: subprocess.run(args, cwd=root, text=True, capture_output=True),
        diagnostics=work / '.diagnostics', label=label)


try:
    containers = run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'])
    volumes = run(['docker', 'volume', 'ls', '--format', '{{.Name}}'])
    labelled = run(['docker', 'volume', 'ls', '--filter', f'label=com.docker.compose.project={project}', '-q'])
    if containers or labelled or any(v.startswith(project + '_') or v in bootstrap.volume_names(project)
                                    for v in volumes.split()):
        raise RuntimeError('drill project already exists; refusing to touch it')
except RuntimeError as error:
    sys.exit(f'FAIL: {error}')

env_file = work / '.env'
command = ['docker', 'compose', '-f', str(root / 'compose.yaml'), '--project-directory', str(root), '--env-file', str(env_file)]
image = next(line.strip().split(':-', 1)[1].removesuffix('}')
             for line in (root / 'compose.yaml').read_text().splitlines()
             if line.strip().startswith('image: ${LG_POSTGRES_IMAGE:-'))


def dc(*args):
    return run(command + list(args))


def remove_pg():
    run(['docker', 'run', '--rm', '--network', 'none', '--user', '0',
         '-v', f'{work}:/work', '--entrypoint', 'sh', image, '-ec', 'rm -rf /work/pg'])


def interrupted(signum, frame):
    raise RuntimeError('drill interrupted')

signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGHUP, interrupted)

try:
    settings = {
        'LG_POSTGRES_DATA_DIR': str(work / 'pg'), 'LG_BACKUP_DIR': str(backup_work / 'backups'),
        'LANGFUSE_INIT_USER_EMAIL': 'drill@gateway.test',
        'LG_HTTP_PORT': port, 'LG_HTTPS_PORT': https_port,
        'LG_VOLUME_PREFIX': project, 'LG_PLATFORM_NETWORK': network, 'LG_PUBLIC_PORT_SUFFIX': ':' + port,
    }
    text = (root / '.env.example').read_text()
    for key, value in settings.items():
        text = re.sub(rf'^{key}=.*$', f'{key}={value}', text, flags=re.M)
    env_file.write_text(text)
    (backup_work / 'backups').mkdir()
    # ClickHouse lists its backups disk root at startup: 0711 is not enough.
    os.chmod(backup_work / 'backups', 0o755)
    run(['docker', 'network', 'create', network], label='drill-network-create')
    network_created = True
    print(f'booting {project} on localhost:{port}', flush=True)
    run(['python3', 'scripts/bootstrap.py', '--env-file', str(env_file)], label='drill-bootstrap')
    env = dict(re.findall(r'^([A-Z][A-Z0-9_]*)=(.*)$', env_file.read_text(), re.M))
    cookies = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    def request(host, path, data=None, headers=None):
        request_headers = {'Host': f'{host}.localhost:{port}', **(headers or {})}
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, headers=request_headers)
        with client.open(req, timeout=60) as response:
            return response.read()

    def generate_key():
        result = json.loads(request('litellm', '/key/generate', b'{"models":["gateway-mock"]}', {
            'Authorization': 'Bearer ' + env['LITELLM_MASTER_KEY'], 'Content-Type': 'application/json',
        }))
        return result['key']

    def completion(message, key):
        payload = json.dumps({'model': 'gateway-mock', 'messages': [{'role': 'user', 'content': message}]}).encode()
        result = json.loads(request('litellm', '/v1/chat/completions', payload, {
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
        }))
        if result['choices'][0]['finish_reason'] != 'stop':
            raise RuntimeError('mock completion did not finish')

    def observations():
        credentials = env['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'] + ':' + env['LANGFUSE_INIT_PROJECT_SECRET_KEY']
        result = json.loads(request('langfuse', '/api/public/v2/observations?limit=100', headers={
            'Authorization': 'Basic ' + base64.b64encode(credentials.encode()).decode(),
        }))
        return {item['id'] for item in result['data'] if item['name'] == 'litellm_request'}

    def wait_observations(expected=None):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            found = observations()
            if expected is not None and expected <= found or expected is None and len(found) == 2:
                return found
            time.sleep(2)
        raise RuntimeError('expected litellm_request observations did not appear')

    def event_objects():
        result = json.loads(dc('run', '--rm', '--no-deps', '-T', 'rustfs-init',
            'aws configure set default.s3.addressing_style path; '
            'aws --endpoint-url http://rustfs:9000 s3api list-objects-v2 '
            '--bucket langfuse --prefix events/ --query "Contents[].{Key: Key, ETag: ETag}" --output json'))
        if not result:
            raise RuntimeError('RustFS has no event objects')
        return {item['Key']: item['ETag'] for item in result}

    dc('run', '--rm', '--no-deps', '-T', 'rustfs-init',
       'printf "media-drill" > /tmp/media; '
       'aws --endpoint-url http://rustfs:9000 s3api put-object --bucket langfuse '
       '--key media/drill --body /tmp/media --content-type image/png '
       "--content-disposition 'inline; filename=\"drill.png\"' >/dev/null")
    key_a = generate_key()
    completion('restore-drill-one', key_a)
    completion('restore-drill-two', key_a)
    expected = wait_observations()
    objects = event_objects()
    print('ok: two completions, both observations and event objects persisted', flush=True)
    for cycle in (1, 2):
        output = run(['scripts/backup.sh', '--env-file', str(env_file)], label='drill-backup')
        print(output, flush=True)
        checkpoint_path = next(line.removeprefix('Checkpoint: ') for line in output.splitlines() if line.startswith('Checkpoint: '))
        captured = json.loads((Path(checkpoint_path) / 'manifest.json').read_text())
        if captured['postgres_timeline_id'] != cycle:
            raise RuntimeError('Checkpoint captured the wrong restored timeline')
        if cycle > 1 and not (Path(checkpoint_path) / 'wal' / f'{cycle:08X}.history').is_file():
            raise RuntimeError('Checkpoint omitted restored timeline history')
        print(f'ok: captured timeline {cycle} and its required history', flush=True)
        dc('up', '-d', '--wait', '--wait-timeout', '300')
        key_b = generate_key()
        start = time.monotonic()
        dc('down', '-v', '--remove-orphans')
        run(['docker', 'volume', 'rm', *bootstrap.volume_names(project)])
        remove_pg()
        recovered = backup_work / f'recovered-backups-{cycle}'
        recovered.mkdir()
        os.chmod(recovered, 0o755)
        env_file.write_text(re.sub(r'^LG_BACKUP_DIR=.*$', f'LG_BACKUP_DIR={recovered}', env_file.read_text(), flags=re.M))
        print(run(['scripts/restore.sh', checkpoint_path, '--env-file', str(env_file)], label='drill-restore'), flush=True)
        wait_observations(expected)
        media = json.loads(dc('run', '--rm', '--no-deps', '-T', 'rustfs-init',
            'aws --endpoint-url http://rustfs:9000 s3api head-object --bucket langfuse --key media/drill'))
        if media.get('ContentType') != 'image/png' or media.get('ContentDisposition') != 'inline; filename="drill.png"':
            raise RuntimeError('restored media metadata differs')
        restored_objects = event_objects()
        if any(restored_objects.get(key) != etag for key, etag in objects.items()):
            raise RuntimeError('restored RustFS event objects differ')
        request('langfuse', '/api/public/health', b'')
        csrf = json.loads(request('langfuse', '/api/auth/csrf'))['csrfToken']
        form = urllib.parse.urlencode({
            'csrfToken': csrf, 'email': env['LANGFUSE_INIT_USER_EMAIL'],
            'password': env['LANGFUSE_INIT_USER_PASSWORD'], 'json': 'true',
            'callbackUrl': f'http://langfuse.localhost:{port}/',
        }).encode()
        request('langfuse', '/api/auth/callback/credentials', form,
                {'Content-Type': 'application/x-www-form-urlencoded'})
        session = json.loads(request('langfuse', '/api/auth/session'))
        if session.get('user', {}).get('email') != env['LANGFUSE_INIT_USER_EMAIL']:
            raise RuntimeError('restored Langfuse login failed')
        request('langfuse', '/project/' + env.get('LANGFUSE_INIT_PROJECT_ID', 'project-default'))
        completion('after-restore', key_a)
        try:
            completion('after-checkpoint-key', key_b)
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise RuntimeError(f'key B returned unexpected HTTP {error.code}') from error
        else:
            raise RuntimeError('key B created after the Checkpoint survived restore')
        print(f'RESTORE DRILL CYCLE {cycle} PASSED: two observations, login, key A accepted, key B rejected, '
              f'event object ETags and media metadata; RTO={time.monotonic() - start:.1f}s', flush=True)
    print('RESTORE DRILL PASSED (2 cycles)', flush=True)
except (RuntimeError, OSError, ValueError, KeyError, StopIteration, KeyboardInterrupt) as error:
    print(f'FAIL: {error}', file=sys.stderr)
    sys.exit(1)
finally:
    print(f'tearing down {project}', flush=True)
    # Do not remove data if teardown failed and a container may still be using it.
    try:
        dc('down', '-v', '--remove-orphans')
        existing = set(run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).split())
        for volume in bootstrap.volume_names(project):
            if volume in existing:
                run(['docker', 'volume', 'rm', volume])
        if (work / '.diagnostics').exists() or any(backup_work.rglob('.diagnostics')):
            print(f'diagnostics and drill files retained in {work} and {backup_work}', file=sys.stderr)
        else:
            run(['docker', 'run', '--rm', '--network', 'none', '--user', '0',
                 '-v', f'{work}:/work', '-v', f'{backup_work}:/backup', '--entrypoint', 'sh', image, '-ec',
                 'rm -rf /work/pg /backup/backups /backup/recovered-backups-1 /backup/recovered-backups-2'])
            shutil.rmtree(work)
            shutil.rmtree(backup_work)
    except (RuntimeError, OSError) as error:
        print(f'cleanup failed; retained {work}: {error}', file=sys.stderr)
        sys.exit(1)
    finally:
        if network_created:
            try:
                run(['docker', 'network', 'rm', network], label='drill-network-remove')
            except (RuntimeError, OSError) as error:
                print(f'cleanup failed: {error}', file=sys.stderr)
                sys.exit(1)
