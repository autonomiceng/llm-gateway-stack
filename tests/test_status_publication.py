"""Host I/O safety, bounded subprocesses and opt-in scheduler installation."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import install_status_timer as installer
import status_io as io
import status_observer as observer
from test_status_observer import AT, FakeRunner


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_existing_restrictive_directory_is_preserved_and_publication_is_atomic(self):
        target = self.root / 'console'
        target.mkdir(mode=0o700)
        with io.directory(target) as fd:
            io.publish(fd, 'status.json', {'first': True})
            with patch.object(io.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    io.publish(fd, 'status.json', {'second': True})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(json.loads((target / 'status.json').read_text()), {'first': True})
        self.assertEqual([p.name for p in target.iterdir()], ['status.json'])

    def test_symlink_directory_and_file_are_refused_without_touching_target(self):
        real = self.root / 'real'
        real.mkdir(mode=0o755)
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            with io.directory(link / 'nested'):
                pass
        self.assertFalse((real / 'nested').exists())
        private = self.root / 'private'
        private.write_text('credentials')
        (real / 'status.json').symlink_to(private)
        with io.directory(real) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {'public': True})
        self.assertEqual(private.read_text(), 'credentials')

    def test_fifo_and_hardlink_and_writable_public_directory_are_refused(self):
        directory = self.root / 'console'
        directory.mkdir(mode=0o755)
        target = directory / 'status.json'
        os.mkfifo(target)
        with io.directory(directory) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {})
        target.unlink()
        other = self.root / 'other'
        other.write_text('private')
        target.hardlink_to(other)
        with io.directory(directory) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {})
        directory.chmod(0o777)
        with self.assertRaises(io.Unavailable):
            with io.directory(directory):
                pass

    def test_bad_json_and_size_limits_do_not_publish_a_partial_document(self):
        for text in ('{"a":1,"a":2}', 'NaN', '1e999', '-1e999', '{broken', '[' * 5000, '"' + 'x' * 65536 + '"'):
            with self.subTest(text=text[:20]), self.assertRaises(io.Unavailable):
                io.read_json(text)
        with io.directory(self.root) as fd:
            io.publish(fd, 'status.json', {'original': True})
            with self.assertRaises(io.Unavailable):
                io.publish(fd, 'status.json', {'large': 'x' * 65536})
        self.assertEqual(json.loads((self.root / 'status.json').read_text()), {'original': True})

    def test_timeout_stdout_stderr_and_exit_failures_are_bounded_and_silent(self):
        for script, options in (
            ('import time; time.sleep(20)', {'timeout': 0.1}),
            ('print("x" * 100000)', {'limit': 1000}),
            ('import sys; sys.stderr.write("secret" * 100000)', {'limit': 1000}),
            ('raise SystemExit(1)', {}),
            ('import os; os.write(1, b"\\xff")', {}),
        ):
            started = time.monotonic()
            with self.subTest(script=script), self.assertRaises(io.Unavailable) as raised:
                io.run([sys.executable, '-c', script], **options)
            self.assertEqual(str(raised.exception), '')
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(io.run([sys.executable, '-c', 'print("ok")']), 'ok\n')
        for code in (125, 126, 127):
            with self.subTest(code=code), self.assertRaises(io.Unsupported):
                io.run([sys.executable, '-c', f'raise SystemExit({code})'])
        with self.assertRaises(io.Unsupported):
            io.run(['/nonexistent-status-probe'])

    def test_deadline_stops_descendant_after_direct_child_exits(self):
        marker = self.root / 'descendant-heartbeat'
        child = "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); " + "\nfor i in range(200): p.write_text(str(i)); time.sleep(.02)"
        parent = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', " + repr(child) + "])"
        with self.assertRaises(io.Unavailable):
            io.run([sys.executable, '-c', parent], timeout=.3)
        self.assertTrue(marker.exists(), 'descendant must execute before the deadline')
        before = marker.read_text()
        time.sleep(.15)
        self.assertEqual(marker.read_text(), before, 'descendant continued after probe cleanup')

    def test_task_records_are_private_and_malformed_records_do_not_break_siblings(self):
        env = self.root / '.env'
        io.task_record(self.root, env, AT, 'healthy')
        record = self.root / 'data/status/bootstrap.json'
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)
        self.assertEqual(io.read_task(self.root, env)['lastExecutionAt'], AT)
        record.write_bytes(b'\xff')
        with self.assertRaises(io.Unavailable):
            io.read_task(self.root, env)
        record.write_text('{bad')
        doc = observer.collect(self.root, env, FakeRunner(), lambda: AT)
        rows = {row['id']: row for row in doc['components']}
        self.assertEqual(rows['bootstrap']['state'], 'unknown')
        self.assertEqual(rows['litellm']['state'], 'healthy')

    def test_installer_selects_explicit_paths_and_is_reversible_without_overwriting(self):
        root = self.root / 'checkout café with $money% and "quotes"'
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/status_observer.py').touch()
        (root / 'compose.yaml').touch()
        env = root / 'selected.env'
        env.touch()
        unit_dir = self.root / 'units'
        calls = []
        installer.install(root, env, unit_dir, lambda argv, **_: calls.append(argv))
        self.assertEqual(calls, [['systemctl', '--user', 'daemon-reload'],
                                 ['systemctl', '--user', 'enable', '--now', 'llm-gateway-status.timer']])
        text = (unit_dir / 'llm-gateway-status.service').read_text()
        self.assertIn('$$money%%', text)
        self.assertIn('\\"quotes\\"', text)
        self.assertIn('--env-file', text)
        self.assertIn('selected.env', text)
        self.assertNotIn('EnvironmentFile', text)
        self.assertIn('OnUnitInactiveSec=30s', (unit_dir / 'llm-gateway-status.timer').read_text())
        calls.clear()
        with self.assertRaises(io.Unavailable):
            installer.install(root, env, unit_dir, lambda argv, **_: calls.append(argv))
        self.assertEqual(calls, [])
        for value in ('path\nExecStart=bad', 'path\x00bad'):
            with self.assertRaises(io.Unavailable):
                installer.quote(value)

    def test_partial_unit_write_rolls_back_without_enabling_timer(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/status_observer.py').touch()
        (self.root / 'compose.yaml').touch()
        env = self.root / '.env'
        env.touch()
        unit_dir = self.root / 'units'
        original = os.open
        def opened(path, *args, **kwargs):
            if path == installer.NAME + '.timer':
                raise OSError('injected second write failure')
            return original(path, *args, **kwargs)
        calls = []
        with patch.object(installer.os, 'open', side_effect=opened), self.assertRaises(OSError):
            installer.install(self.root, env, unit_dir, lambda *args, **kwargs: calls.append(args))
        self.assertEqual(list(unit_dir.iterdir()), [])
        self.assertEqual(calls, [])

    def test_xdg_relative_and_empty_values_use_home_config(self):
        argv = ['installer', '--checkout', str(self.root), '--env-file', str(self.root / '.env'), '--install']
        for value in ('', 'relative', str(self.root / 'absolute')):
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': value}), patch.object(sys, 'argv', argv), \
                    patch.object(installer, 'install') as install, patch('builtins.print'):
                self.assertEqual(installer.main(), 0)
            expected = Path(value) if Path(value).is_absolute() else Path.home() / '.config'
            self.assertEqual(install.call_args.args[2], expected / 'systemd/user')
    def test_config_environment_does_not_inherit_shell_selection_or_credentials(self):
        with patch.dict(os.environ, {'LG_LITELLM_IMAGE': 'secret', 'COMPOSE_FILE': 'other',
                                     'OPENAI_API_KEY': 'private', 'DOCKER_HOST': 'unix:///socket'}, clear=True):
            self.assertEqual(observer.environment(), {'DOCKER_HOST': 'unix:///socket'})


if __name__ == '__main__':
    unittest.main()
