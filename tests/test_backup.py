"""Backup contracts; fake runners never call Docker."""

import json
import copy
import fcntl
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import checkpoint as backup  # noqa: E402
import bootstrap  # noqa: E402


def runtime_fixture(root):
    stack = backup.Stack.__new__(backup.Stack)
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
            output = 'sha256:' + argv[3].split('@')[0].split(':')[0]
            if '.RepoDigests' in argv[-1]:
                output += ' ' + json.dumps([argv[3].split('@')[0].split(':')[0] + '@sha256:' + 'a' * 64])
        elif argv == stack.command + ['ps', '--status', 'running', '--services']:
            output = ' '.join(services)
        else:
            raise AssertionError(f'unexpected command: {argv}')
        return subprocess.CompletedProcess(argv, 0, output, '')

    stack.runner = runner
    return stack, containers, calls


class BackupTests(unittest.TestCase):
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
                        backup.backup(stack, False, 300)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                    self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

    def test_capture_resolves_effective_images_and_refuses_local_only_before_fencing(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, containers, calls = runtime_fixture(Path(directory))
            for service in ('postgres', 'caddy'):
                stack.config['services'][service]['image'] = service + ':trial'
                container = next(c for c in containers
                                 if c['Config']['Labels']['com.docker.compose.service'] == service)
                container['Config']['Image'] = service + ':trial'
            stack.attest_runtime()
            self.assertEqual(stack.images['postgres'], 'postgres@sha256:' + 'a' * 64)
            self.assertTrue(all(backup.immutable(ref) for ref in stack.images.values()))
            runner = stack.runner
            def local_only(argv):
                result = runner(argv)
                if '.RepoDigests' in argv[-1] and argv[3] == 'postgres:trial':
                    result.stdout = 'sha256:postgres []'
                return result
            stack.runner = local_only
            calls.clear()
            with self.assertRaisesRegex(RuntimeError, 'no registry digest'):
                backup.backup(stack, False, 300)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

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
                        backup.backup(stack, False, 300)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                    self.assertFalse(any('stop' in call or 'exec' in call for call in calls))

    def check_secret_refusal(self, content, shell, expected):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text(content)
            original_stack = backup.Stack

            def locked_stack(path):
                with path.with_name(path.name + '.lock').open('w') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return original_stack(path)

            with patch.dict(os.environ, shell, clear=True), \
                 patch.object(sys, 'argv', ['checkpoint.py', 'restore', directory, '--env-file', str(env)]), \
                 patch.object(backup, 'Stack', side_effect=locked_stack), \
                 patch.object(backup.subprocess, 'Popen') as runner:
                with self.assertRaisesRegex((RuntimeError, bootstrap.Refused), expected):
                    backup.main()
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
            checkpoint = root / 'checkpoint'
            checkpoint.mkdir()
            (checkpoint / 'artifact').write_bytes(b'payload')
            env = root / '.env'
            values = {'PASSWORD': 'unique-secret-value', 'CUSTOM_SETTING': 'private-setting-value'}
            env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
            images = {'postgres': 'mirror/store@sha256:' + 'b' * 64}
            doc = backup.manifest(checkpoint, images, env, 'abc123',
                                  '2026-09-17T01:02:03Z', True, 'checkpoint_test', 7)
            (checkpoint / 'manifest.json').write_text(json.dumps(doc))
            parsed = json.loads((checkpoint / 'manifest.json').read_text())
            self.assertEqual(parsed['images'], images)
            self.assertEqual(parsed['env_keys'], sorted(values))
            self.assertEqual(parsed['postgres_timeline_id'], 7)
            for value in values.values():
                self.assertNotIn(value, json.dumps(parsed))
            self.assertEqual(parsed['artifacts']['artifact']['size'], 7)
            self.assertEqual(parsed['artifacts'], backup.inventory(checkpoint))
            (checkpoint / 'artifact').write_bytes(b'changed')
            self.assertNotEqual(parsed['artifacts'], backup.inventory(checkpoint))

    def test_restore_refuses_unfenced_checkpoint_without_explicit_flag(self):
        stack = Mock()
        stack.images = {}
        stack.env_file = ROOT / '.env.example'
        stack.config = {'networks': {'platform': {'name': 'drill-platform'}}}
        stack.data.exists.side_effect = RuntimeError('restore proceeded')
        with patch.object(backup, 'verify_checkpoint', return_value={'fenced': False}), \
             patch.object(bootstrap, 'ensure_network'):

            with self.assertRaisesRegex(RuntimeError, '--allow-unfenced'):
                backup.restore(stack, ROOT / 'unused-checkpoint')
            stack.data.exists.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'restore proceeded'):
                backup.restore(stack, ROOT / 'unused-checkpoint', allow_unfenced=True)
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
                            backup.check_empty(data, 'drill', 'postgres:pinned', runner,
                                               diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))
                    else:
                        backup.check_empty(data, 'drill', 'postgres:pinned', runner,
                                           diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))
                        self.assertEqual(sum('--mount' in call for call in calls), 5)
                    self.assertFalse(any('src=other_postgres,' in str(call) for call in calls))
            def running(argv):
                return subprocess.CompletedProcess(argv, 0, 'running-container', '')
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                backup.check_empty(data, 'drill', 'postgres:pinned', running,
                                   diagnostics=data / '.diagnostics', volumes=bootstrap.volume_names('drill'))

    def test_fence_wait_requires_sustained_idle_and_times_out_when_busy(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        polls = iter([1, 0, 1, 0, 0, 0])
        backup.wait_idle(lambda: next(polls), timeout=20, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 10)
        with self.assertRaisesRegex(RuntimeError, 'timeout'):
            backup.wait_idle(lambda: 1, timeout=6, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 16)
        def broken():
            raise RuntimeError('poll unavailable')
        with self.assertRaisesRegex(RuntimeError, 'poll unavailable'):
            backup.wait_idle(broken)

    def test_restore_creates_missing_directories_then_refuses_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'compose.yaml').write_text((ROOT / 'compose.yaml').read_text())
            stack = backup.Stack.__new__(backup.Stack)
            stack.data = root / 'pg'
            stack.backups = root / 'backups'
            stack.project = 'drill'
            stack.env_file = ROOT / '.env.example'
            stack.config = {'networks': {'platform': {'name': 'drill-platform'}}}
            stack.prefix = 'drill'
            stack.volumes = bootstrap.volume_names('drill')
            stack.images = {'postgres': 'postgres:pinned'}
            checked_mounts = []

            def runner(argv):
                code = 0
                if '--mount' in argv:
                    checked_mounts.append(argv[argv.index('--mount') + 1])
                    self.assertTrue(stack.data.is_dir())
                    self.assertEqual(stack.data.stat().st_mode & 0o777, 0o755)
                    code = int(any(stack.data.iterdir()))
                return subprocess.CompletedProcess(argv, code, '', '')

            stack.runner = runner
            with patch.object(backup, 'ROOT', root), \
                 patch.object(backup, 'verify_checkpoint', return_value={'fenced': True}), \
                 patch.object(stack, 'helper', side_effect=RuntimeError('empty targets verified')) as helper:
                old_umask = os.umask(0o077)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'empty targets verified'):
                        backup.restore(stack, root / 'checkpoint')
                finally:
                    os.umask(old_umask)
                self.assertTrue((root / 'data/console/versions.json').is_file())
                helper.assert_called_once()
                helper.reset_mock()
                (stack.data / 'PG_VERSION').write_text('18')
                with self.assertRaisesRegex(RuntimeError, 'restore refused'):
                    backup.restore(stack, root / 'checkpoint')
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
                        output = backup.Stack.WORKER_DONE + '\nPrisma connection has been closed.\nShutdown complete'
                    elif argv[-1] == 'SHOW archive_mode':
                        output = 'on'
                    elif argv[-2:-1] == ['-c'] or 'EVAL' in ' '.join(argv):
                        output = '0'
                    return subprocess.CompletedProcess(argv, 0, output, '')

                stack.runner = runner
                with patch.object(backup, 'wait_idle'), patch.object(backup, 'health'), patch.object(backup, 'wait_healthy'):
                    with self.assertRaisesRegex(RuntimeError, f'service {failed} did not stop cleanly \\(exit 137\\)'):
                        backup.backup(stack, False, 300)
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

    def test_failed_command_retains_private_diagnostics_without_argv_in_name(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = Path(directory) / '.diagnostics'
            diagnostics.mkdir(mode=0o755)
            argv = ['private-executable', '--secret-option', 'secret-value']

            def runner(args):
                self.assertEqual(args, argv)
                return subprocess.CompletedProcess(args, 1, 'private stdout\n', 'private stderr\n')

            with self.assertRaises(RuntimeError) as raised:
                backup.checked(argv, runner, diagnostics=diagnostics, label='capture')
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


if __name__ == '__main__':
    unittest.main()
