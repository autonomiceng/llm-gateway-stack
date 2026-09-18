"""Production readiness contracts with fake Docker and S3 responses."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap
import checkpoint
import destroy


class ProductionReadinessTests(unittest.TestCase):
    def test_external_volumes_use_prefix_and_only_missing_volumes_are_created(self):
        names = bootstrap.volume_names('isolated')
        calls = []

        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '\n'.join(names[:2]), '')

        bootstrap.ensure_volumes(runner, 'isolated')
        self.assertEqual(calls[1:], [['docker', 'volume', 'create', name] for name in names[2:]])
        compose = (ROOT / 'compose.yaml').read_text()
        for suffix in bootstrap.VOLUMES:
            self.assertIn(f'  {suffix}:\n    external: true\n    name: ${{LG_VOLUME_PREFIX:-llm-gateway-stack}}-{suffix}', compose)
        with tempfile.TemporaryDirectory() as directory:
            found = bootstrap.installation_state(Path(directory), Path(directory) / 'absent', runner,
                                                  project='different', prefix='isolated')
        self.assertEqual(found, [f'volume {name}' for name in names[:2]])

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
