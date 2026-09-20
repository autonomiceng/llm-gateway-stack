"""Persistence review regressions with real files and fake command runners."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap
import checkpoint


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


class PersistenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = self.root / '.env'
        self.env.write_text(''.join(f'{key}=saved-value\n' for key in bootstrap.MANAGED))
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

        for extra in (['--stop-timeout', '120'], ['--stop-timeout', '1', '--no-fence']):
            with self.subTest(extra=extra), patch.object(checkpoint, 'ROOT', self.root), \
                 patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--env-file', str(self.env), *extra]), \
                 patch.object(checkpoint, 'Stack', side_effect=RuntimeError('preflight reached')) as stack, \
                 patch.object(checkpoint, 'write_metrics'):
                with self.assertRaisesRegex(RuntimeError, 'preflight reached'):
                    checkpoint.main()
                stack.assert_called_once_with(self.env)

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
        stack = Mock(data=data, backups=backups, images=doc['images'], env_file=env_file,
                     config={'networks': {'platform': {'name': 'drill-platform'}}})
        copytree = shutil.copytree

        def corrupt_copy(src, dest, *args, **kwargs):
            copytree(src, dest, *args, **kwargs)
            if Path(src) == source:
                (dest / 'objects/media/saved').write_bytes(b'corruption')

        with patch.object(checkpoint, 'ROOT', self.root), \
             patch.object(bootstrap, 'write_versions'), patch.object(bootstrap, 'ensure_volumes'), patch.object(bootstrap, 'ensure_network'), \
             patch.object(checkpoint, 'check_empty'), patch.object(shutil, 'copytree', side_effect=corrupt_copy):
            with self.assertRaisesRegex(RuntimeError, 'restore copy checksum'):
                checkpoint.restore(stack, source)
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



if __name__ == '__main__':
    unittest.main()
