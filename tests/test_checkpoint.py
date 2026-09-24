"""Checkpoint capture, restore, retention and drill preflight; fake runners never call Docker."""

import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap  # noqa: E402
import checkpoint  # noqa: E402
import destroy  # noqa: E402


def runtime_fixture(root):
    stack = checkpoint.Stack.__new__(checkpoint.Stack)
    stack.backups = root
    stack.project = 'drill'
    stack.command = ['docker', 'compose']
    services = ('caddy', 'litellm', 'langfuse-web', 'langfuse-worker',
                'postgres', 'clickhouse', 'rustfs', 'valkey')
    stack.config = {'services': {name: {'image': name + ':new@sha256:' + 'a' * 64, 'volumes': []}
                                for name in services},
                    'volumes': {'valkey-data': {'name': 'drill-valkey-data'}}}
    stack.config['services']['postgres']['volumes'] = [
        {'type': 'bind', 'source': '/new/pg', 'target': '/var/lib/postgresql'}]
    stack.config['services']['valkey']['volumes'] = [
        {'type': 'volume', 'source': 'valkey-data', 'target': '/data'}]
    containers = [{'Config': {'Image': name + ':new@sha256:' + 'a' * 64,
                             'Labels': {'com.docker.compose.service': name}},
                   'Image': 'sha256:' + name, 'Mounts': []} for name in services]
    containers[-4]['Mounts'] = [{'Type': 'bind', 'Source': '/new/pg',
                                'Destination': '/var/lib/postgresql', 'RW': True}]
    containers[-1]['Mounts'] = [{'Type': 'volume', 'Name': 'drill-valkey-data',
                                'Destination': '/data', 'RW': True}]
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:3] == ['docker', 'ps', '-aq']:
            output = ' '.join(services)
        elif argv[:2] == ['docker', 'inspect']:
            output = json.dumps(containers)
        elif argv[:3] == ['docker', 'image', 'inspect']:
            repository = argv[3].split('@', 1)[0]
            if ':' in repository.rsplit('/', 1)[-1]:
                repository = repository.rsplit(':', 1)[0]
            output = 'sha256:' + repository
            if '.RepoDigests' in argv[-1]:
                output += ' ' + json.dumps([repository + '@sha256:' + 'a' * 64])
        elif argv == stack.command + ['ps', '--status', 'running', '--services']:
            output = ' '.join(services)
        else:
            raise AssertionError(f'unexpected command: {argv}')
        return subprocess.CompletedProcess(argv, 0, output, '')

    stack.runner = runner
    return stack, containers, calls


class CaptureAndRestoreTests(unittest.TestCase):
    def test_capture_refuses_changed_image_before_writing_or_fencing(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, containers, calls = runtime_fixture(Path(directory))
            stack.config['services']['postgres']['image'] = 'postgres:trial'
            containers[-4]['Config']['Image'] = 'postgres:trial'
            stack.attest_runtime()
            original = copy.deepcopy(containers)
            for field in ('reference', 'content'):
                with self.subTest(field=field):
                    containers[:] = copy.deepcopy(original)
                    if field == 'reference':
                        containers[-4]['Config']['Image'] = 'postgres:old@sha256:old'
                    else:
                        containers[-4]['Image'] = 'sha256:old'
                    calls.clear()
                    with self.assertRaisesRegex(RuntimeError, 'image.*differs'):
                        checkpoint.backup(stack, 300)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                    self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

    def test_capture_resolves_effective_images_and_refuses_unverifiable_content_before_fencing(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, containers, calls = runtime_fixture(Path(directory))
            for service in ('postgres', 'caddy'):
                stack.config['services'][service]['image'] = service + ':trial'
                container = next(c for c in containers
                                 if c['Config']['Labels']['com.docker.compose.service'] == service)
                container['Config']['Image'] = service + ':trial'
            out, warning = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(warning):
                stack.attest_runtime()
            self.assertEqual(out.getvalue(), '')
            self.assertIn('publication was not checked', warning.getvalue())
            self.assertEqual(stack.images['postgres'], 'postgres@sha256:' + 'a' * 64)
            self.assertTrue(all(checkpoint.immutable(ref) for ref in stack.images.values()))
            runner = stack.runner
            def local_only(argv):
                result = runner(argv)
                if '.RepoDigests' in argv[-1] and argv[3] == 'postgres:trial':
                    result.stdout = 'sha256:postgres []'
                return result
            stack.runner = local_only
            calls.clear()
            with self.assertRaisesRegex(RuntimeError, 'no verifiable immutable reference'):
                checkpoint.backup(stack, 300)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

    def test_capture_prefers_configured_registry_when_image_has_multiple_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, containers, calls = runtime_fixture(Path(directory))
            reference = 'registry.example:5000/store:trial'
            expected = 'registry.example:5000/store@sha256:' + 'a' * 64
            other = 'another.example/store@sha256:' + 'a' * 64
            stack.config['services']['postgres']['image'] = reference
            container = next(c for c in containers if c['Config']['Labels']['com.docker.compose.service'] == 'postgres')
            container['Config']['Image'] = reference
            runner = stack.runner
            def inspect(argv):
                if argv[:3] == ['docker', 'image', 'inspect'] and argv[3] in (reference, expected, other):
                    value = container['Image']
                    if '.RepoDigests' in argv[-1]:
                        value += ' ' + json.dumps([other, expected])
                    return subprocess.CompletedProcess(argv, 0, value, '')
                return runner(argv)
            stack.runner = inspect
            stack.attest_runtime()
            self.assertEqual(stack.images['postgres'], expected)
            for ref in ('postgres:trial', 'docker.io/library/postgres:trial', 'index.docker.io/library/postgres@sha256:abc'):
                self.assertEqual(checkpoint.image_repository(ref), 'postgres')

    def test_capture_refuses_changed_mounts_before_writing_or_fencing(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, containers, calls = runtime_fixture(Path(directory))
            original = copy.deepcopy(containers)
            for service, field, value in ((-4, 'Source', '/old/pg'),
                                          (-1, 'Name', 'old-valkey-data'), (-1, 'RW', False)):
                with self.subTest(field=field):
                    containers[:] = copy.deepcopy(original)
                    containers[service]['Mounts'][0][field] = value
                    calls.clear()
                    with self.assertRaisesRegex(RuntimeError, 'mounts differ'):
                        checkpoint.backup(stack, 300)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                    self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

    def check_secret_refusal(self, content, shell, expected):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text(content)
            original_stack = checkpoint.Stack

            def locked_stack(path):
                with path.with_name(path.name + '.lock').open('w') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return original_stack(path)

            with patch.dict(os.environ, shell, clear=True), \
                 patch.object(sys, 'argv', ['checkpoint.py', 'restore', directory, '--env-file', str(env)]), \
                 patch.object(checkpoint, 'Stack', side_effect=locked_stack), \
                 patch.object(checkpoint.subprocess, 'Popen') as runner:
                with self.assertRaisesRegex((RuntimeError, bootstrap.Refused), expected):
                    checkpoint.main()
                runner.assert_not_called()
            self.assertEqual(env.read_text(), content)
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {'.env', '.env.lock'})

    def test_restore_requires_saved_nonempty_secrets_even_when_supplied_by_shell(self):
        saved = {key: 'saved-value' for key in bootstrap.MANAGED}
        for value in (None, ''):
            with self.subTest(value=value):
                partial = dict(saved)
                if value is None:
                    partial.pop('LANGFUSE_ENCRYPTION_KEY')
                else:
                    partial['LANGFUSE_ENCRYPTION_KEY'] = value
                self.check_secret_refusal(''.join(f'{k}={v}\n' for k, v in partial.items()),
                                          {'LANGFUSE_ENCRYPTION_KEY': 'from-shell'}, 'missing managed')

    def test_restore_rejects_conflicting_and_duplicate_saved_secrets(self):
        content = ''.join(f'{key}=saved-value\n' for key in bootstrap.MANAGED)
        for key in ('LANGFUSE_ENCRYPTION_KEY', 'LANGFUSE_SALT', 'LITELLM_SALT_KEY'):
            with self.subTest(key=key):
                self.check_secret_refusal(content, {key: 'replacement'}, 'shell secrets differ')
        self.check_secret_refusal(content + 'VALKEY_PASSWORD=other\n', {}, 'env_repair_required')

    def test_manifest_records_every_image_and_keys_without_env_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'checkpoint'
            capture.mkdir()
            (capture / 'artifact').write_bytes(b'payload')
            env = root / '.env'
            values = {'PASSWORD': 'unique-secret-value', 'CUSTOM_SETTING': 'private-setting-value'}
            env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
            images = {'postgres': 'mirror/store@sha256:' + 'b' * 64}
            doc = checkpoint.manifest(capture, images, env, 'abc123',
                                  '2026-09-17T01:02:03Z', True, 'checkpoint_test', 7)
            (capture / 'manifest.json').write_text(json.dumps(doc))
            parsed = json.loads((capture / 'manifest.json').read_text())
            self.assertEqual(parsed['images'], images)
            self.assertEqual(parsed['env_keys'], sorted(values))
            self.assertEqual(parsed['postgres_timeline_id'], 7)
            for value in values.values():
                self.assertNotIn(value, json.dumps(parsed))
            self.assertEqual(parsed['artifacts']['artifact']['size'], 7)
            self.assertEqual(parsed['artifacts'], checkpoint.inventory(capture))
            (capture / 'artifact').write_bytes(b'changed')
            self.assertNotEqual(parsed['artifacts'], checkpoint.inventory(capture))

    def test_restore_refuses_unfenced_checkpoint_without_explicit_flag(self):
        stack = Mock()
        stack.images = {}
        stack.settings = {}
        stack.env_file = ROOT / '.env.example'
        stack.config = {'networks': {'platform': {'name': 'drill-platform'}}}
        stack.data.exists.side_effect = RuntimeError('restore proceeded')
        with patch.object(checkpoint, 'verify_checkpoint', return_value={'fenced': False}), \
             patch.object(bootstrap, 'ensure_network'):

            with self.assertRaisesRegex(RuntimeError, '--allow-unfenced'):
                checkpoint.restore(stack, ROOT / 'unused-checkpoint')
            stack.data.exists.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'restore proceeded'):
                checkpoint.restore(stack, ROOT / 'unused-checkpoint', allow_unfenced=True)
            stack.data.exists.assert_called_once()

    def test_restore_refuses_nonempty_postgres_or_any_project_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            for dirty in (str(data), 'drill_valkey-data', 'drill_caddy-data', 'drill-rustfs-data', 'custom-volume', None):
                calls = []

                def runner(argv):
                    calls.append(argv)
                    output, code = '', 0
                    if argv[:3] == ['docker', 'volume', 'ls']:
                        output = ('custom-volume' if '--filter' in argv else
                                  'drill_valkey-data\ndrill_caddy-data\ndrill-rustfs-data\nother_postgres')
                    if '--mount' in argv and dirty:
                        code = int(f'src={dirty},' in argv[argv.index('--mount') + 1])
                    return subprocess.CompletedProcess(argv, code, output, '')

                with self.subTest(dirty=dirty):
                    if dirty:
                        with self.assertRaisesRegex(RuntimeError, 'restore refused'):
                            checkpoint.check_empty(data, 'drill', 'postgres:pinned', runner,
                                               diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))
                    else:
                        checkpoint.check_empty(data, 'drill', 'postgres:pinned', runner,
                                           diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))
                        self.assertEqual(sum('--mount' in call for call in calls), 5)
                    self.assertFalse(any('src=other_postgres,' in str(call) for call in calls))
            def running(argv):
                return subprocess.CompletedProcess(argv, 0, 'running-container', '')
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                checkpoint.check_empty(data, 'drill', 'postgres:pinned', running,
                                   diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))

    def test_fence_wait_requires_sustained_idle_and_times_out_when_busy(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        polls = iter([1, 0, 1, 0, 0, 0])
        checkpoint.wait_idle(lambda: next(polls), timeout=20, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 10)
        with self.assertRaisesRegex(RuntimeError, 'timeout'):
            checkpoint.wait_idle(lambda: 1, timeout=6, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 16)
        def broken():
            raise RuntimeError('poll unavailable')
        with self.assertRaisesRegex(RuntimeError, 'poll unavailable'):
            checkpoint.wait_idle(broken)

    def test_restore_creates_missing_directories_then_refuses_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'compose.yaml').write_text((ROOT / 'compose.yaml').read_text())
            stack = checkpoint.Stack.__new__(checkpoint.Stack)
            stack.data = root / 'pg'
            stack.backups = root / 'backups'
            stack.project = 'drill'
            stack.env_file = ROOT / '.env.example'
            stack.settings = {}
            stack.config = {'networks': {'platform': {'name': 'drill-platform'}}}
            stack.prefix = 'drill'
            stack.volumes = bootstrap.volume_names('drill')
            stack.images = {'postgres': 'postgres:pinned'}
            checked_mounts = []
            # An earlier restore under umask 077 left the console directory unreadable to Caddy.
            (root / 'data/console').mkdir(parents=True, mode=0o700)

            def runner(argv):
                code = 0
                if '--mount' in argv:
                    checked_mounts.append(argv[argv.index('--mount') + 1])
                    self.assertTrue(stack.data.is_dir())
                    self.assertEqual(stack.data.stat().st_mode & 0o777, 0o755)
                    code = int(any(stack.data.iterdir()))
                return subprocess.CompletedProcess(argv, code, '', '')

            stack.runner = runner
            with patch.object(checkpoint, 'ROOT', root), \
                 patch.object(checkpoint, 'verify_checkpoint', return_value={'fenced': True}), \
                 patch.object(bootstrap, 'ensure_network'), \
                 patch.object(stack, 'helper', side_effect=RuntimeError('empty targets verified')) as helper:
                old_umask = os.umask(0o077)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'empty targets verified'):
                        checkpoint.restore(stack, root / 'checkpoint')
                finally:
                    os.umask(old_umask)
                # Caddy mounts this directory; Docker would create a missing one owned by root,
                # and restore's umask must not hide it from Caddy.
                self.assertEqual((root / 'data/console').stat().st_mode & 0o777, 0o755)
                helper.assert_called_once()
                helper.reset_mock()
                (stack.data / 'PG_VERSION').write_text('18')
                with self.assertRaisesRegex(RuntimeError, 'restore refused'):
                    checkpoint.restore(stack, root / 'checkpoint')
                helper.assert_not_called()
                self.assertEqual((stack.data / 'PG_VERSION').read_text(), '18')
                self.assertEqual(len(checked_mounts), 2)

    def test_unclean_fence_stop_refuses_manifest_and_resumes_services(self):
        services = ('caddy', 'litellm', 'langfuse-web', 'langfuse-worker', 'valkey')
        # Only the stores that hold unflushed data must stop cleanly (see Stack.stop).
        for failed in ('caddy', 'litellm', 'valkey'):
            with self.subTest(service=failed), tempfile.TemporaryDirectory() as directory:
                stack, _, _ = runtime_fixture(Path(directory))
                inspect_runner = stack.runner
                calls = []

                def runner(argv, timeout=None):
                    if 'up' in argv:
                        self.assertEqual(timeout, 120)
                    calls.append(argv)
                    if argv[:2] != ['docker', 'compose']:
                        return inspect_runner(argv)
                    args = argv[2:]
                    output = ''
                    if args[:2] == ['ps', '--status']:
                        output = ' '.join((*services, 'postgres', 'clickhouse', 'rustfs'))
                    elif args[:2] == ['ps', '-a']:
                        service = args[-1]
                        output = json.dumps({'Service': service, 'State': 'exited', 'ExitCode': 137 if service == failed else 0})
                    elif args[:1] == ['logs']:
                        output = checkpoint.Stack.WORKER_DONE + '\nPrisma connection has been closed.\nShutdown complete\nApplication shutdown complete.'
                    elif argv[-1] == 'SHOW archive_mode':
                        output = 'on'
                    elif argv[-2:-1] == ['-c'] or 'EVAL' in ' '.join(argv):
                        output = '0'
                    return subprocess.CompletedProcess(argv, 0, output, '')

                stack.runner = runner
                with patch.object(checkpoint, 'wait_idle'), patch.object(checkpoint, 'health'), patch.object(checkpoint, 'wait_healthy'):
                    with self.assertRaisesRegex(RuntimeError, f'service {failed} did not stop cleanly \\(exit 137, state .*\\)'):
                        checkpoint.backup(stack, 300)
                self.assertFalse(list(stack.backups.rglob('manifest.json')))
                stopped = services[:services.index(failed) + 1]
                self.assertEqual(calls[-1], ['docker', 'compose', 'up', '-d', '--no-deps', '--no-recreate', *reversed(stopped)])
                self.assertFalse(any('pg_basebackup' in call for call in calls))

    def test_langfuse_web_requires_completed_backend_close_since_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, _, _ = runtime_fixture(Path(directory))
            with patch.object(stack, 'dc', side_effect=['', 'SIGTERM received']) as dc:
                with self.assertRaisesRegex(RuntimeError, 'langfuse-web did not finish'):
                    stack.stop('langfuse-web', 120)
                self.assertIn('--since', dc.call_args.args)
            with patch.object(stack, 'dc', side_effect=['', 'Prisma connection has been closed.\nShutdown complete']):
                stack.stop('langfuse-web', 120)

    def test_litellm_requires_zero_exit_and_completed_lifespan_since_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, _, _ = runtime_fixture(Path(directory))
            for logs in ('', 'Waiting for application shutdown.'):
                with self.subTest(logs=logs), patch.object(stack, 'dc', side_effect=[
                        '', json.dumps({'State': 'exited', 'ExitCode': 0}), logs]) as dc:
                    with self.assertRaisesRegex(RuntimeError, 'litellm did not finish'):
                        stack.stop('litellm', 120)
                    self.assertIn('--since', dc.call_args.args)
            with patch.object(stack, 'dc', side_effect=[
                    '', json.dumps({'State': 'exited', 'ExitCode': 0}),
                    'INFO:     Application shutdown complete.']):
                stack.stop('litellm', 120)
            with patch.object(stack, 'dc', side_effect=[
                    '', json.dumps({'State': 'exited', 'ExitCode': 143})]):
                with self.assertRaisesRegex(RuntimeError, 'exit 143'):
                    stack.stop('litellm', 120)

    def test_failed_command_retains_private_diagnostics_without_argv_in_name(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = Path(directory) / '.diagnostics'
            diagnostics.mkdir(mode=0o755)
            argv = ['private-executable', '--secret-option', 'secret-value']

            def runner(args):
                self.assertEqual(args, argv)
                return subprocess.CompletedProcess(args, 1, 'private stdout\n', 'private stderr\n')

            with self.assertRaises(RuntimeError) as raised:
                checkpoint.checked(argv, runner, diagnostics=diagnostics, label='capture')
            logs = list(diagnostics.iterdir())
            self.assertEqual(len(logs), 1)
            path = logs[0]
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.read_text(), 'private stdout\nprivate stderr\n')
            self.assertRegex(path.name, r'^\d{8}T\d{12}Z-capture\.log$')
            self.assertIn(str(path), str(raised.exception))
            self.assertIn('capture failed', str(raised.exception))
            for token in argv:
                self.assertNotIn(token, path.name)
                self.assertNotIn(token, str(raised.exception))



class StopDiagnosticTests(unittest.TestCase):
    def test_zero_exit_with_running_state_remains_a_fence_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, _, _ = runtime_fixture(Path(directory))
            with patch.object(stack, 'dc', side_effect=['', json.dumps({'State': 'running', 'ExitCode': 0})]):
                with self.assertRaisesRegex(RuntimeError, "exit 0, state 'running'"):
                    stack.stop('litellm', 10)


def checkpoint_fixture(root, member=None):
    """Build a v1 fixture independently of the production manifest/inventory writers."""
    root.mkdir()
    for directory in ('postgres', 'clickhouse', 'objects/media', 'wal'):
        (root / directory).mkdir(parents=True)
    for name in ('postgres/base.tar', 'postgres/pg_wal.tar', 'valkey.tar'):
        with tarfile.open(root / name, 'w') as archive:
            item = member if member is not None and name == 'postgres/base.tar' else tarfile.TarInfo('safe')
            item.size = 0
            archive.addfile(item)
    (root / 'postgres/backup_manifest').write_text('{}')
    (root / 'clickhouse/backup.zip').write_bytes(b'clickhouse-fixture')
    (root / 'objects/media/saved').write_bytes(b'saved media')
    (root / 'objects.meta.json').write_text('{"media/saved":{"ContentType":"image/png"}}')
    (root / 'wal/000000010000000000000001').write_bytes(b'wal-fixture')
    artifacts = {}
    for path in root.rglob('*'):
        if path.is_file():
            payload = path.read_bytes()
            artifacts[path.relative_to(root).as_posix()] = {
                'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    doc = {'version': 1, 'images': {'postgres': 'postgres:pinned@sha256:' + 'a' * 64}, 'clickhouse_database': 'default',
           'postgres_restore_point': 'checkpoint_20260918T010000000000Z',
           'postgres_timeline_id': 1, 'fenced': True, 'artifacts': artifacts}
    (root / 'manifest.json').write_text(json.dumps(doc))
    return doc


def separate_device(backups):
    """Model a separately mounted repository without requiring host mounts."""
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == backups:
            fields = list(result)
            fields[2] += 1
            return os.stat_result(fields)
        return result

    return patch.object(Path, 'stat', stat)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = self.root / '.env'
        # Proxy Mode refuses same-filesystem backups unless the operator opts in.
        self.env.write_text('LG_ACCESS_MODE=proxy\n' + ''.join(f'{key}=saved-value\n' for key in bootstrap.MANAGED))
        self.old_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask)
        self.tmp.cleanup()

    def test_storage_refused_before_compose_and_diagnostics_never_create_repository(self):
        backups = self.root / 'not-mounted' / 'backups'
        with self.env.open('a') as handle:
            handle.write(f'LG_BACKUP_DIR={backups}\nLG_POSTGRES_DATA_DIR={self.root / "pg/new"}\n')
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, 'private-output', 'private-error'))
        with patch.dict(os.environ, {}, clear=True), patch.object(checkpoint, 'ROOT', self.root):
            with self.assertRaisesRegex(RuntimeError, 'must exist'):
                checkpoint.Stack(self.env, runner)
            runner.assert_not_called()
            self.assertFalse(backups.parent.exists())
            with self.assertRaisesRegex(RuntimeError, 'compose failed.*diagnostics unavailable'):
                checkpoint.checked(['compose'], runner, diagnostics=backups / '.diagnostics', label='compose')
            self.assertFalse(backups.parent.exists())
            backups.mkdir(parents=True)
            runner.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'different filesystems'):
                checkpoint.Stack(self.env, runner)
            runner.assert_not_called()
            self.assertFalse((backups / '.diagnostics').exists())
            with separate_device(backups):
                with self.assertRaisesRegex(RuntimeError, 'compose failed'):
                    checkpoint.Stack(self.env, runner)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(len(list((backups / '.diagnostics').glob('*.log'))), 1)

    def test_same_filesystem_policy_still_refuses_invalid_values_and_overlapping_paths(self):
        backups = self.root / 'backups'
        backups.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(backups, target_is_directory=True)
        original = self.env.read_text()
        key = 'LG_ALLOW_SAME_FILESYSTEM_BACKUP'
        for policy, data, expected in (
            ('false', self.root / 'pg', 'different filesystems'),
            ('TRUE', self.root / 'pg', 'invalid_backup_policy'),
            ('1', self.root / 'pg', 'invalid_backup_policy'),
            ('', self.root / 'pg', 'invalid_backup_policy'),
            ('true', backups, 'must not overlap'),
            ('true', backups / 'pg', 'must not overlap'),
            ('true', self.root, 'must not overlap'),
            ('true', alias / 'pg', 'must not overlap'),
        ):
            with self.subTest(policy=policy, data=data):
                self.env.write_text(original + f'LG_BACKUP_DIR={backups}\nLG_POSTGRES_DATA_DIR={data}\n'
                                    f'{key}={policy}\n')
                runner = Mock()
                with patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex((RuntimeError, bootstrap.Refused), expected):
                        checkpoint.Stack(self.env, runner)
                runner.assert_not_called()
                self.assertFalse((backups / '.diagnostics').exists())

    def test_owned_command_timeout_and_interrupt_clean_up_process_group(self):
        popen = subprocess.Popen
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                pid_file = self.root / 'child.pid'
                pid_file.unlink(missing_ok=True)
                processes = []
                # The child inherits the owned session, as a Compose plugin would.
                code = ('import subprocess, sys, time; '
                        'from pathlib import Path; '
                        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                        'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')

                def spawn(*args, **kwargs):
                    process = popen(*args, **kwargs)
                    processes.append(process)
                    if interrupted:
                        def communicate(**_):
                            deadline = time.monotonic() + 5
                            while not pid_file.exists() and time.monotonic() < deadline:
                                time.sleep(0.01)
                            raise KeyboardInterrupt()
                        process.communicate = communicate
                    return process

                try:
                    with patch.object(checkpoint.subprocess, 'Popen', side_effect=spawn):
                        with self.assertRaises(KeyboardInterrupt if interrupted else RuntimeError) as raised:
                            checkpoint.run([sys.executable, '-c', code, str(pid_file), 'private-secret'], timeout=1)
                    self.assertNotIn('private-secret', str(raised.exception))
                    self.assertEqual(processes[0].returncode, -signal.SIGKILL)
                    self.assertTrue(pid_file.exists())
                    child = int(pid_file.read_text())
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            state = Path(f'/proc/{child}/stat').read_text().split()[2]
                        except (FileNotFoundError, ProcessLookupError):
                            break
                        if state == 'Z':
                            break
                        self.assertLess(time.monotonic(), deadline, 'owned descendant survived cleanup')
                        time.sleep(0.01)
                finally:
                    for process in processes:
                        try:
                            if process.returncode is None:
                                os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()

    def test_fenced_stop_timeout_rejected_before_locks_or_stack_construction(self):
        for seconds in ('-1', '0', '119'):
            with self.subTest(seconds=seconds), \
                 patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--stop-timeout', seconds,
                                           '--env-file', str(self.env)]), \
                 patch.object(checkpoint, 'Stack') as stack, redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    checkpoint.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('must be at least 120', errors.getvalue())
                stack.assert_not_called()
                self.assertFalse(self.env.with_name('.env.lock').exists())

        with patch.object(checkpoint, 'ROOT', self.root), \
             patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--env-file', str(self.env), '--stop-timeout', '120']), \
             patch.object(checkpoint, 'Stack', side_effect=RuntimeError('preflight reached')) as stack, \
             patch.object(checkpoint, 'write_metrics'):
            with self.assertRaisesRegex(RuntimeError, 'preflight reached'):
                checkpoint.main()
            stack.assert_called_once_with(self.env)

    def test_capture_has_no_unfenced_option(self):
        with patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--help']), \
             patch.object(checkpoint, 'Stack') as stack, redirect_stdout(io.StringIO()) as usage:
            with self.assertRaises(SystemExit) as raised:
                checkpoint.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(set(re.findall(r'--[a-z-]+', usage.getvalue())),
                         {'--help', '--fence-timeout', '--stop-timeout', '--env-file'})
        with patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--unfenced', '--env-file', str(self.env)]), \
             redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            checkpoint.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('unrecognized arguments', errors.getvalue())
        stack.assert_not_called()
        self.assertFalse(self.env.with_name('.env.lock').exists())

    def test_preflight_failure_sets_failure_but_lock_contenders_preserve_status(self):
        backups = self.root / 'backups'
        backups.mkdir()
        with patch.object(checkpoint, 'ROOT', self.root), patch.object(checkpoint.time, 'time', return_value=123), \
             patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--env-file', str(self.env)]):
            checkpoint.write_metrics(True)
            metrics = self.root / 'data/console/metrics.txt'
            before = metrics.read_bytes()
            with patch.object(checkpoint, 'Stack', side_effect=RuntimeError('preflight failed')):
                with self.assertRaisesRegex(RuntimeError, 'preflight failed'):
                    checkpoint.main()
            self.assertIn('lg_checkpoint_success 0', metrics.read_text())
            self.assertIn('lg_checkpoint_timestamp_seconds 123', metrics.read_text())
            for lock_path in (self.env.with_name(self.env.name + '.lock'), self.root / 'data/console/.checkpoint.lock',
                              backups / '.checkpoint.lock'):
                with self.subTest(lock=lock_path):
                    metrics.write_bytes(before)
                    with lock_path.open('w') as held:
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        with patch.object(checkpoint, 'Stack', return_value=Mock(backups=backups)), \
                             patch.object(checkpoint, 'backup') as capture:
                            with self.assertRaises(BlockingIOError):
                                checkpoint.main()
                            capture.assert_not_called()
                    self.assertEqual(metrics.read_bytes(), before)

    def test_copied_checkpoint_corruption_is_refused_before_extraction(self):
        source = self.root / 'source'
        doc = checkpoint_fixture(source)
        backups = self.root / 'backups'
        backups.mkdir()
        data = self.root / 'pg'
        data.mkdir()
        env_file = self.root / '.env'
        env_file.write_text('LG_BACKUP_DIR=backups\n')
        stack = Mock(data=data, backups=backups, images=doc['images'], env_file=env_file, settings={},
                     project='restore-project', prefix='restore-data', config={'networks': {'platform': {'name': 'drill-platform'}}})
        stack.runner.return_value = subprocess.CompletedProcess([], 0, '', '')
        copytree = shutil.copytree

        def corrupt_copy(src, dest, *args, **kwargs):
            copytree(src, dest, *args, **kwargs)
            if Path(src) == source:
                (dest / 'objects/media/saved').write_bytes(b'corruption')

        with patch.object(checkpoint, 'ROOT', self.root), \
             patch.object(bootstrap, 'ensure_network'), \
             patch.object(checkpoint, 'check_empty'), patch.object(shutil, 'copytree', side_effect=corrupt_copy):
            with self.assertRaisesRegex(RuntimeError, 'restore copy checksum'):
                checkpoint.restore(stack, source)
        created = [call.args[0] for call in stack.runner.call_args_list
                   if call.args[0][:3] == ['docker', 'volume', 'create']]
        self.assertEqual({command[-1] for command in created}, set(bootstrap.volume_names('restore-data')))
        self.assertTrue(all(command[command.index('--label') + 1] == 'com.docker.compose.project=restore-project'
                            for command in created))
        # Only the archive-empty preflight helper ran. No extraction or store startup.
        self.assertFalse((backups / doc['postgres_restore_point'].removeprefix('checkpoint_')).exists())
        stack.helper.assert_called_once()
        self.assertIn('entries=$(ls -A /backup/archive)', stack.helper.call_args.args[1])
        stack.dc.assert_not_called()
        self.assertEqual((source / 'objects/media/saved').read_bytes(), b'saved media')
        self.assertEqual(list(data.iterdir()), [])

    def test_resumption_health_and_primary_errors_gate_pruning_and_success(self):
        for mode in ('capture', 'resume', 'both', 'health', 'worker-unhealthy', 'success'):
            with self.subTest(mode=mode):
                backups = self.root / mode
                backups.mkdir()
                events = []
                stack = Mock(backups=backups, env_file=self.env)
                stack.pg.side_effect = lambda sql: ('on' if sql == 'SHOW archive_mode' else
                    '0' if sql.startswith('SELECT count') else '000000010000000000000001')
                stack.queue_count.return_value = 0

                def dc(*args, **kwargs):
                    if args[0] == 'ps' and '--format' in args:
                        return json.dumps([{'Service': service, 'State': 'exited' if mode in ('resume', 'both') else 'running',
                            'Health': 'unhealthy' if mode == 'worker-unhealthy' and service == 'langfuse-worker' else 'healthy'}
                            for service in ('caddy', 'litellm', 'langfuse-web', 'langfuse-worker', 'valkey')])
                    if args[0] == 'ps':
                        return 'caddy litellm langfuse-web langfuse-worker valkey postgres clickhouse rustfs'
                    if args[0] == 'up':
                        events.append('resume')
                        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                            self.assertEqual(signal.getsignal(sig), signal.SIG_IGN)
                        self.assertEqual(kwargs['label'], 'fence-resume')
                        self.assertEqual(args[1:], ('-d', '--no-deps', '--no-recreate', 'valkey', 'langfuse-worker', 'langfuse-web', 'litellm', 'caddy'))
                        if mode in ('resume', 'both'):
                            raise RuntimeError('fence-resume failed')
                    return ''

                def helper(*args, **kwargs):
                    if mode in ('capture', 'both'):
                        raise RuntimeError('archive capture failed')

                def health(_, timeout=None):
                    self.assertEqual(timeout, 120)
                    events.append('health')
                    if mode == 'health':
                        raise RuntimeError('resumed health failed')

                stack.dc.side_effect = dc
                stack.helper.side_effect = helper
                with patch.object(checkpoint, 'ROOT', self.root), \
                     patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--env-file', str(self.env)]), \
                     patch.object(checkpoint, 'Stack', return_value=stack), \
                     patch.object(checkpoint, 'manifest', return_value={}), \
                     patch.object(checkpoint, 'checked', return_value='revision'), \
                     patch.object(checkpoint, 'wait_idle'), \
                     patch.object(checkpoint, 'health', side_effect=health), \
                     patch.object(checkpoint, 'prune', side_effect=lambda _: events.append('prune')), \
                     patch.object(checkpoint.time, 'monotonic', side_effect=iter([0, 0, 1000, 1000, 1000])), \
                     patch.object(checkpoint.time, 'time', return_value=456), redirect_stdout(io.StringIO()):
                    checkpoint.write_metrics(True)
                    if mode == 'success':
                        checkpoint.main()
                        self.assertEqual(events, ['resume', 'health', 'prune'])
                    else:
                        with self.assertRaises(RuntimeError) as error:
                            checkpoint.main()
                        if mode in ('capture', 'both'):
                            self.assertIn('archive capture failed', str(error.exception))
                        if mode in ('resume', 'both'):
                            self.assertIn('did not become healthy', str(error.exception))
                        self.assertNotIn('prune', events)
                    metrics = (self.root / 'data/console/metrics.txt').read_text()
                    self.assertIn(f'lg_checkpoint_success {int(mode == "success")}', metrics)
                    self.assertIn('lg_checkpoint_timestamp_seconds 456', metrics)

    def test_resume_retries_transient_status_and_delayed_stop(self):
        stack = Mock()
        healthy = json.dumps([{'Service': 'langfuse-worker', 'State': 'running', 'Health': 'healthy'}])
        stack.dc.side_effect = [RuntimeError('temporary daemon error'), '[]', '', healthy]
        with patch.object(checkpoint.time, 'sleep'), patch.object(checkpoint.time, 'monotonic', return_value=0):
            checkpoint.wait_healthy(stack, ['langfuse-worker'])
        self.assertEqual([call.args[0] for call in stack.dc.call_args_list], ['ps', 'ps', 'up', 'ps'])

        now = [0]
        def hung_status(*args, timeout, **kwargs):
            now[0] += timeout
            raise RuntimeError('command timed out')
        stack.dc.reset_mock(side_effect=True)
        stack.dc.side_effect = hung_status
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(RuntimeError, 'did not become healthy'):
                checkpoint.wait_healthy(stack, ['langfuse-worker'], timeout=5)
        self.assertEqual(now[0], 5)
        self.assertEqual(stack.dc.call_count, 1)

    def test_resume_waits_out_a_late_stop_after_initial_healthy_status(self):
        stack = Mock()
        healthy = json.dumps([{'Service': 'valkey', 'State': 'running', 'Health': 'healthy'}])
        stack.dc.side_effect = [healthy, healthy, '[]', '', healthy]
        now = [0]
        def sleep(_):
            now[0] += 30
        with patch.object(checkpoint.time, 'sleep', side_effect=sleep), \
             patch.object(checkpoint.time, 'monotonic', side_effect=lambda: now[0]):
            checkpoint.wait_healthy(stack, ['valkey'], settle=70)
        self.assertEqual([call.args[0] for call in stack.dc.call_args_list], ['ps', 'ps', 'ps', 'up', 'ps'])

    def test_command_child_has_separate_session(self):
        result = checkpoint.run([sys.executable, '-c', 'import os; print(os.getsid(0))'])
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(int(result.stdout), os.getsid(0))

    def test_media_sidecar_tracks_only_synced_files(self):
        objects = self.root / 'set/objects'
        (objects / 'media').mkdir(parents=True)
        (objects / 'media/synced').write_bytes(b'synced')
        stack = checkpoint.Stack.__new__(checkpoint.Stack)
        stack.backups = self.root

        def helper(service, script, *args, **kwargs):
            if 'xargs' in script:
                parts = self.root / 'set/objects.meta.parts'
                self.assertEqual((parts / 'keys').read_bytes(), b'0\0media/synced\0')
                (parts / '0.json').write_text('{"ContentType":"image/png"}')
            return ''

        stack.helper = Mock(side_effect=helper)
        stack.objects('backup', '/backup/set/objects')
        self.assertEqual(json.loads((objects.parent / 'objects.meta.json').read_text()),
                         {'media/synced': {'ContentType': 'image/png'}})
        stack.objects('restore', '/backup/set/objects')

    def test_verify_checkpoint_rejects_tampering_extra_files_symlinks_and_missing_timeline(self):
        for case in ('valid', 'checksum', 'extra', 'symlink', 'timeline'):
            with self.subTest(case=case):
                source = self.root / case
                doc = checkpoint_fixture(source)
                if case == 'checksum':
                    (source / 'objects/media/saved').write_bytes(b'other media')
                elif case == 'extra':
                    (source / 'unlisted-file').write_bytes(b'extra')
                elif case == 'symlink':
                    (source / 'link').symlink_to(source / 'objects/media/saved')
                elif case == 'timeline':
                    del doc['postgres_timeline_id']
                    (source / 'manifest.json').write_text(json.dumps(doc))
                if case == 'valid':
                    self.assertEqual(checkpoint.verify_checkpoint(source, doc['images']), doc)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'checksum|symlinks|timeline'):
                        checkpoint.verify_checkpoint(source, doc['images'])

    def test_restore_requires_manifest_immutable_identity_before_mutation(self):
        source = self.root / 'source'
        doc = checkpoint_fixture(source)
        self.assertEqual(checkpoint.verify_checkpoint(source, doc['images']), doc)
        for ref in ('postgres:trial', 'postgres@sha256:' + 'b' * 64):
            stack = Mock(images={'postgres': ref})
            with self.subTest(ref=ref), patch.object(bootstrap, 'ensure_network') as network:
                with self.assertRaisesRegex(RuntimeError, 'same immutable image'):
                    checkpoint.restore(stack, source)
                network.assert_not_called()
                stack.dc.assert_not_called()
                stack.data.mkdir.assert_not_called()
        doc['images']['postgres'] = 'postgres:trial'
        (source / 'manifest.json').write_text(json.dumps(doc))
        with self.assertRaisesRegex(RuntimeError, 'same immutable image'):
            checkpoint.verify_checkpoint(source, doc['images'])

    def test_restore_refuses_missing_image_before_creating_target_storage(self):
        source = self.root / 'source'
        doc = checkpoint_fixture(source)
        stack = Mock(images=doc['images'], backups=self.root / 'backups')
        stack.backups.mkdir()
        stack.runner.return_value = subprocess.CompletedProcess([], 1, '', 'image unavailable')
        with patch.object(bootstrap, 'ensure_network') as network:
            with self.assertRaisesRegex(RuntimeError, 'restore-image'):
                checkpoint.restore(stack, source)
        network.assert_not_called()
        stack.data.mkdir.assert_not_called()
        stack.helper.assert_not_called()

    def test_verify_checkpoint_rejects_hostile_tar_members_with_matching_hashes(self):
        for index, (name, kind) in enumerate((('../escape', tarfile.REGTYPE),
                ('/absolute', tarfile.REGTYPE), ('symlink', tarfile.SYMTYPE),
                ('hardlink', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE))):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside'
                source = self.root / str(index)
                doc = checkpoint_fixture(source, member)
                with self.assertRaisesRegex(RuntimeError, 'unsupported archive member'):
                    checkpoint.verify_checkpoint(source, doc['images'])

    def test_restore_retargeting_still_resolves_shell_storage_and_warns_by_name(self):
        backups = self.root / 'target-backups'
        backups.mkdir()
        (self.root / 'compose.yaml').write_text((ROOT / 'compose.yaml').read_text())
        images = {'postgres': 'mirror/store@sha256:' + 'a' * 64}
        config = {'name': 'target-project', 'volumes': {'valkey-data': {'name': 'target-prefix-valkey-data'}},
                  'services': {name: {'image': image} for name, image in images.items()}}
        config['services']['postgres']['volumes'] = [
            {'target': '/backup', 'source': str(backups)},
            {'target': '/var/lib/postgresql', 'source': str(self.root / 'target-pg')}]
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(config), ''))
        overrides = {'LG_BACKUP_DIR': str(backups), 'LG_POSTGRES_DATA_DIR': str(self.root / 'target-pg'),
                     'LG_VOLUME_PREFIX': 'target-prefix', 'COMPOSE_PROJECT_NAME': 'target-project'}
        for distinct in (True, False):
            if not distinct:
                overrides['LG_ALLOW_SAME_FILESYSTEM_BACKUP'] = 'true'
            with self.subTest(distinct=distinct):
                with patch.dict(os.environ, overrides, clear=True), patch.object(checkpoint, 'ROOT', self.root), \
                     (separate_device(backups) if distinct else nullcontext()):
                    stack = checkpoint.Stack(self.env, runner)
                    self.assertEqual(stack.backups, backups)
                    self.assertEqual(stack.data, self.root / 'target-pg')
                    self.assertEqual(stack.project, 'target-project')
                    self.assertEqual(stack.images, images)
                    self.assertEqual(stack.prefix, 'target-prefix')
                    self.assertEqual(set(stack.storage_overrides), set(overrides))
                    output = io.StringIO()
                    with patch.object(checkpoint, 'Stack', return_value=stack), patch.object(checkpoint, 'restore') as restore, \
                         patch.object(sys, 'argv', ['checkpoint.py', 'restore', 'source', '--env-file', str(self.env)]), \
                         redirect_stderr(output):
                        checkpoint.main()
                    restore.assert_called_once()
                    for key in overrides:
                        self.assertIn(key, output.getvalue())
                    self.assertNotIn(str(backups), output.getvalue())
                    self.assertNotIn('saved-value', output.getvalue())
                    self.assertNotIn('target-prefix', self.env.read_text())


    def test_restore_creates_the_platform_network_with_the_env_file_allocation(self):
        backups = self.root / 'backups'
        backups.mkdir()
        with self.env.open('a') as handle:
            handle.write(f'LG_BACKUP_DIR={backups}\nLG_POSTGRES_DATA_DIR={self.root / "pg"}\n'
                         'LG_ALLOW_SAME_FILESYSTEM_BACKUP=true\n'
                         'LG_PLATFORM_SUBNET=10.44.0.0/24\nLG_PLATFORM_IP_RANGE=10.44.0.128/25\n')
        config = {'name': 'drill', 'volumes': {}, 'networks': {'platform': {'name': 'drill-platform'}},
                  'services': {'postgres': {'image': 'postgres@sha256:' + 'a' * 64, 'volumes': [
                      {'target': '/backup', 'source': str(backups)},
                      {'target': '/var/lib/postgresql', 'source': str(self.root / 'pg')}]}}}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(config), ''))
        # Nothing in the shell: the allocation comes from the env file alone.
        with patch.dict(os.environ, {}, clear=True), patch.object(checkpoint, 'ROOT', self.root), \
             redirect_stderr(io.StringIO()):
            stack = checkpoint.Stack(self.env, runner)
            with patch.object(checkpoint, 'verify_checkpoint', return_value={'fenced': True}), \
                 patch.object(bootstrap, 'ensure_network', side_effect=RuntimeError('network checked')) as network:
                with self.assertRaisesRegex(RuntimeError, 'network checked'):
                    checkpoint.restore(stack, self.root / 'checkpoint')
        network.assert_called_once_with(runner, 'drill-platform', '10.44.0.0/24', '10.44.0.128/25')

    def test_restore_publishes_the_status_document_after_readiness(self):
        backups = self.root / 'backups'
        stamp = '20260918T010000000000Z'
        (backups / stamp).mkdir(parents=True)
        pinned = {name: f'example/{name}:1.0@sha256:' + 'c' * 64
                  for name in ('caddy', 'litellm', 'postgres', 'valkey', 'valkey-exporter')}
        every = {name: {'image': ref} for name, ref in pinned.items()}
        selected = {name: dict(service) for name, service in every.items() if name != 'valkey-exporter'}
        selected['caddy'].update(environment={'LG_ACCESS_MODE': 'local', 'LG_PUBLIC_DOMAIN': 'gateway.test',
                                              'LG_LITELLM_URL': 'https://litellm.gateway.test'},
                                 ports=[{'target': 80, 'published': 18090, 'host_ip': '127.0.0.1'}])
        calls = []

        def runner(argv, **options):
            calls.append(argv)
            if argv[-5:] == ['--profile', '*', 'config', '--format', 'json']:
                return subprocess.CompletedProcess(argv, 0, json.dumps({'services': every}), '')
            return subprocess.CompletedProcess(argv, 0, '', '')

        stack = Mock(data=self.root / 'pg', backups=backups, env_file=self.env, settings={}, project='drill',
                     prefix='drill', volumes=[], command=['docker', 'compose'], runner=runner,
                     images={name: service['image'] for name, service in selected.items()},
                     config={'networks': {'platform': {'name': 'drill-platform'}}, 'services': selected})
        stack.pg.return_value = 'f'
        status = self.root / 'data/console/status.json'

        def ready(_):
            self.assertFalse(status.exists(), 'status published before readiness')

        doc = {'fenced': True, 'postgres_restore_point': 'checkpoint_' + stamp, 'postgres_timeline_id': 1,
               'artifacts': {}}
        with patch.object(checkpoint, 'ROOT', self.root), patch.object(checkpoint, 'verify_checkpoint', return_value=doc), \
             patch.object(bootstrap, 'ensure_network'), patch.object(checkpoint, 'check_empty'), \
             patch.object(checkpoint, 'health', side_effect=ready) as health, redirect_stdout(io.StringIO()):
            checkpoint.restore(stack, backups / stamp)
        health.assert_called_once_with(stack)
        published = json.loads(status.read_text())
        self.assertEqual((published['contract'], published['stack']), (2, 'gateway'))
        self.assertEqual({c['id']: c['enabled'] for c in published['components']},
                         {'caddy': True, 'litellm': True, 'postgres': True, 'valkey': True, 'valkey-exporter': False})
        self.assertEqual({c['id']: c.get('url') for c in published['components']}['litellm'], 'https://litellm.gateway.test')
        self.assertNotIn('sha256', status.read_text())
        self.assertFalse((self.root / 'data/console/versions.json').exists())


class DrillPreflightTests(unittest.TestCase):
    def run_preflight(self, root, backup_root):
        scripts = root / 'scripts'
        scripts.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / 'scripts/backup-drill.py', scripts / 'backup-drill.py')
        # PATH is empty: reaching Docker would fail with a different error.
        result = subprocess.run([sys.executable, str(scripts / 'backup-drill.py')],
                                env={'PATH': '', 'SMOKE_BACKUP_ROOT': str(backup_root)},
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / '.scratch').exists())
        return result.stderr

    def test_drill_refuses_checkout_state_before_creating_files_or_calling_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.env', 'data', 'compose.override.yaml'):
                with self.subTest(name=name):
                    path = root / name
                    path.write_text('keep this installation state')
                    self.assertIn('clean disposable checkout', self.run_preflight(root, root))
                    self.assertEqual(path.read_text(), 'keep this installation state')
                    path.unlink()

    def test_drill_checks_configured_backup_root_before_creating_files_or_calling_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIn('existing directory', self.run_preflight(root, root / 'missing'))
            self.assertIn('separate from checkout', self.run_preflight(root, root))


class RetentionMediaAndDestroyTests(unittest.TestCase):
    def test_retention_keeps_n_and_prunes_only_wal_below_oldest_retained_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'archive'
            archive.mkdir()
            files = ['000000010000000000000001', '000000010000000000000002',
                     '000000020000000000000002.partial', '000000010000000000000003',
                     '000000020000000000000004', '00000001.history', '.archive-in-progress',
                     '000000010000000000000002.00000028.backup']
            for name in files:
                (archive / name).touch()
            stamps = []
            for day in range(1, 5):
                stamp = f'202609{day:02}T000000000000Z'
                stamps.append(stamp)
                dest = root / stamp
                (dest / 'postgres').mkdir(parents=True)
                (dest / 'manifest.json').write_text('{}')
                (dest / 'postgres/backup_manifest').write_text(json.dumps({
                    'WAL-Ranges': [{'Timeline': 1, 'Start-LSN': f'0/{day:X}000028'}]}))
                if day == 1:
                    self.assertEqual(checkpoint.retention_plan(root, 1, files, 16 << 20), ([], []))
            incomplete = root / '20260905T000000000000Z'
            incomplete.mkdir()
            stack = checkpoint.Stack.__new__(checkpoint.Stack)
            stack.backups, stack.keep = root, 2
            stack.pg = lambda sql: str(16 << 20)

            def helper(service, script, *args):
                if script.startswith('find'):
                    return '\n'.join(files)
                for name in args:
                    path = root / Path(name).relative_to('/backup')
                    if script.startswith('rm -rf'):
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                return ''

            stack.helper = helper
            checkpoint.prune(stack)
            self.assertEqual(sorted(p.name for p in root.iterdir() if (p / 'manifest.json').exists()), stamps[-2:])
            self.assertEqual(sorted(p.name for p in archive.iterdir()), sorted([
                files[3], files[4], files[5], files[6]]))
            self.assertTrue(incomplete.exists())

    def test_media_metadata_sidecar_round_trip_with_fake_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            objects = root / 'checkpoint/objects'
            (objects / 'media').mkdir(parents=True)
            key = 'media/space and \'quote.png'
            (objects / key).write_bytes(b'png')
            head = {'ContentType': 'image/png', 'ContentDisposition': 'inline; filename="a.png"',
                    'ContentEncoding': 'gzip', 'ETag': 'ignored'}
            stack = checkpoint.Stack.__new__(checkpoint.Stack)
            stack.backups = root
            calls = []

            def helper(service, script, *args, **kwargs):
                calls.append(script)
                if 'list-objects-v2' in script:
                    return json.dumps({'Contents': [{'Key': key}]})
                if 'xargs' in script:
                    self.assertIn('-P 8', script)
                    parts = root / 'checkpoint/objects.meta.parts'
                    self.assertEqual((parts / 'keys').read_bytes(), f'0\0{key}\0'.encode())
                    (parts / '0.json').write_text(json.dumps(head))
                return ''

            stack.helper = helper
            stack.objects('backup', '/backup/checkpoint/objects')
            sidecar = json.loads((objects.parent / 'objects.meta.json').read_text())
            self.assertEqual(sidecar, {key: {k: v for k, v in head.items() if k != 'ETag'}})
            calls.clear()
            stack.objects('restore', '/backup/checkpoint/objects')
            self.assertIn('--content-type application/json', calls[0])
            args = shlex.split(calls[1])
            self.assertIn('s3://langfuse/' + key, args)
            for flag, field in (('--content-type', 'ContentType'), ('--content-disposition', 'ContentDisposition'),
                                ('--content-encoding', 'ContentEncoding')):
                self.assertEqual(args[args.index(flag) + 1], head[field])
            (objects.parent / 'objects.meta.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'sidecar differ'):
                stack.objects('restore', '/backup/checkpoint/objects')

    def test_destroy_requires_typed_project_and_refuses_nonempty_postgres(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('COMPOSE_PROJECT_NAME=disposable\n')
            for answer in ('', 'wrong-project'):
                result = subprocess.run([str(ROOT / 'scripts/destroy.sh'), '--env-file', str(env)],
                                        input=answer + '\n', text=True, capture_output=True,
                                        env={k: v for k, v in os.environ.items() if k != 'COMPOSE_PROJECT_NAME'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('project name did not match', result.stderr)
            config = {'name': 'disposable', 'services': {'postgres': {
                'image': 'postgres:pinned', 'volumes': [{'target': '/var/lib/postgresql', 'source': directory}]}}}
            with patch.dict(os.environ, {'COMPOSE_PROJECT_NAME': 'disposable'}), \
                 patch.object(sys, 'argv', ['destroy.py', '--env-file', str(env)]), \
                 patch('builtins.input', return_value='disposable'), \
                 patch.object(destroy, 'checked', side_effect=[json.dumps(config), RuntimeError('nonempty')]) as docker:
                with self.assertRaisesRegex(RuntimeError, '--include-postgres'):
                    destroy.main()
                self.assertEqual(docker.call_count, 2)


if __name__ == '__main__':
    unittest.main()
