"""Retiring the version 1 status timer. systemctl is a stub; no user manager is touched."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "retire-status-timer.sh"


class RetireStatusTimerTests(unittest.TestCase):
    def test_retirement_is_shellcheck_clean_and_idempotent_on_a_fake_home(self):
        # validate.sh requires shellcheck; a bare interpreter still runs the behaviour check.
        if shutil.which("shellcheck"):
            subprocess.run(["shellcheck", "--shell=sh", str(SCRIPT)], check=True)
        for present in (("llm-gateway-status.service", "llm-gateway-status.timer"), ("llm-gateway-status.service",)):
            with self.subTest(present=present):
                self.retire_twice(present)

    def retire_twice(self, present):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout, home, bin_dir = root / "checkout", root / "home", root / "bin"
            (checkout / "scripts").mkdir(parents=True)
            shutil.copy(SCRIPT, checkout / "scripts")
            units = home / ".config/systemd/user"
            units.mkdir(parents=True)
            for name in (*present, "other.timer"):
                (units / name).write_text("[Unit]\n")
            (checkout / "data/status").mkdir(parents=True)
            (checkout / "data/status/bootstrap.json").write_text("{}")
            (checkout / "data/console").mkdir()
            for name in (".status.lock", ".checkpoint.lock", "metrics.txt", "status.json"):
                (checkout / "data/console" / name).write_text("")
            bin_dir.mkdir()
            log = root / "systemctl.log"
            # Like systemctl, refuse a unit that has no file.
            (bin_dir / "systemctl").write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
for unit in "$@"; do
  case "$unit" in *.timer|*.service) [ -e "{units}/$unit" ] || exit 1;; esac
done
""")
            (bin_dir / "systemctl").chmod(0o755)
            env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
            runs = [subprocess.run(["sh", str(checkout / "scripts/retire-status-timer.sh")], env=env,
                                   capture_output=True, text=True, check=True) for _ in range(2)]

            self.assertEqual(log.read_text().splitlines(), [
                "--user disable --now " + " ".join(sorted(present, reverse=True)),
                "--user daemon-reload"])
            self.assertEqual(sorted(p.name for p in units.iterdir()), ["other.timer"])
            self.assertFalse((checkout / "data/status").exists())
            self.assertEqual(sorted(p.name for p in (checkout / "data/console").iterdir()),
                             [".checkpoint.lock", "metrics.txt", "status.json"])
            self.assertIn("removed", runs[0].stdout)
            self.assertNotIn("removed", runs[1].stdout)
            self.assertIn("no llm-gateway-status units", runs[1].stdout)


if __name__ == "__main__":
    unittest.main()
