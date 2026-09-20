"""Validate generated scheduler units when systemd tooling is available."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import install_status_timer as installer


class UnitSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd tooling unavailable')
    def test_generated_user_units_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for name, contents in installer.units(root, root / '.env').items():
                path = root / name
                path.write_text(contents, encoding='utf-8')
                paths.append(str(path))
            result = subprocess.run(['systemd-analyze', '--user', 'verify', *paths],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
