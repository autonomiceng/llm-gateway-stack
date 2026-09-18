"""Bootstrap contract. Docker is never called; a fake runner answers instead."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bootstrap  # noqa: E402


def runner_with(volumes=(), network_exists=True):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "ls"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(volumes), "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if network_exists else 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    run.calls = calls
    return run


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = self.root / ".env"
        self.template = Path(__file__).resolve().parent.parent / ".env.example"

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, **kwargs):
        return bootstrap.bootstrap(
            ["--env-file", str(self.env), "--template", str(self.template), "--render-only"],
            **kwargs,
        )

    def test_fresh_render_generates_every_secret_once_and_locks_the_file_down(self):
        self.assertEqual(self.render(), 0)
        text = self.env.read_text()
        for key in bootstrap.MANAGED:
            self.assertEqual(text.count(f"\n{key}="), 1, key)
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), "0o600")
        self.assertIn("pk-lf-", text)
        self.assertIn("sk-lf-", text)

    def test_second_render_keeps_existing_secrets_and_unmanaged_lines(self):
        self.render()
        before = self.env.read_text()
        self.env.write_text(before + "MY_CUSTOM=1\n")
        self.render()
        after = self.env.read_text()
        self.assertTrue(after.startswith(before))
        self.assertIn("MY_CUSTOM=1", after)

    def test_duplicate_managed_key_is_refused_not_repaired(self):
        self.env.write_text("VALKEY_PASSWORD=a\nVALKEY_PASSWORD=b\n")
        with self.assertRaises(bootstrap.Refused) as ctx:
            self.render()
        self.assertEqual(ctx.exception.code, "env_repair_required")

    def test_installation_state_sees_postgres_bind_path_and_legacy_volumes(self):
        data = self.root / "pg"
        data.mkdir()
        (data / "PG_VERSION").write_text("18")
        found = bootstrap.installation_state(
            self.root, data,
            runner_with(volumes=["llm-gateway-stack_valkey-data", "llm-gateway-smoke_valkey-data", "other_x"]),
        )
        self.assertEqual(len(found), 2, "another project's volumes are not this installation")
        self.assertTrue(any("postgres data" in f for f in found))
        self.assertTrue(any("valkey-data" in f for f in found))

    def test_versions_json_reads_tags_from_compose(self):
        compose = Path(__file__).resolve().parent.parent / "compose.yaml"
        bootstrap.write_versions(self.root, compose)
        doc = json.loads((self.root / "data" / "console" / "versions.json").read_text())
        self.assertEqual(doc["images"]["langfuse"], "4.37.0")
        self.assertNotIn("sha256", json.dumps(doc))
        self.assertFalse(doc["images"]["litellm"].startswith("v"))

    def test_installation_state_follows_compose_project_name(self):
        found = bootstrap.installation_state(
            self.root, self.root / "missing",
            runner_with(volumes=["llm-gateway-stack_valkey-data", "llm-gateway-smoke_valkey-data"]),
            project="llm-gateway-smoke",
        )
        self.assertEqual(found, ["volume llm-gateway-smoke_valkey-data"])

    def test_project_name_precedence_shell_then_env_file_then_default(self):
        self.assertEqual(bootstrap.project_name({}), "llm-gateway-stack")
        self.assertEqual(bootstrap.project_name({"COMPOSE_PROJECT_NAME": "'review-existing'"}), "review-existing")
        os.environ["COMPOSE_PROJECT_NAME"] = "from-shell"
        try:
            self.assertEqual(bootstrap.project_name({"COMPOSE_PROJECT_NAME": "from-file"}), "from-shell")
        finally:
            del os.environ["COMPOSE_PROJECT_NAME"]

    def test_export_and_quoted_assignments_are_read(self):
        self.env.write_text('export VALKEY_PASSWORD=abc\nLG_POSTGRES_DATA_DIR="/srv/pg"\n')
        lines, values = bootstrap.read_env(self.env)
        self.assertEqual(values["VALKEY_PASSWORD"], "abc")
        settings = {m.group("key"): bootstrap.unquote(m.group("value")) for m in map(bootstrap.ENV_LINE.match, lines) if m}
        self.assertEqual(settings["LG_POSTGRES_DATA_DIR"], "/srv/pg")

    def test_shell_secret_is_persisted_on_fresh_render_and_conflict_is_refused(self):
        os.environ["VALKEY_PASSWORD"] = "from-shell"
        try:
            self.render()
            self.assertIn("VALKEY_PASSWORD=from-shell", self.env.read_text())
            os.environ["VALKEY_PASSWORD"] = "changed"
            with self.assertRaises(bootstrap.Refused) as ctx:
                self.render()
            self.assertEqual(ctx.exception.code, "shell_env_conflict")
            self.assertNotIn("changed", ctx.exception.detail)
        finally:
            del os.environ["VALKEY_PASSWORD"]

    def test_backup_directory_on_postgres_filesystem_is_refused(self):
        self.render()
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        with self.env.open("a") as handle:
            handle.write(f"\nLG_BACKUP_DIR={backup_dir}\nLG_POSTGRES_DATA_DIR={self.root / 'pg'}\n"
                         "LANGFUSE_INIT_USER_EMAIL=operator@gateway.test\n")
        runner = runner_with()
        with patch.object(bootstrap.shutil, "which", return_value="docker"):
            with self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner)
        self.assertEqual(raised.exception.code, "backup_dir_same_filesystem")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.root / "pg").exists())

    def test_langfuse_login_is_required_before_startup(self):
        self.render()
        original = self.env.read_text()
        for email in ("", "admin@example.com", "admin@EXAMPLE.COM"):
            with self.subTest(email=email):
                self.env.write_text(original + f"\nLANGFUSE_INIT_USER_EMAIL={email}\n")
                runner = runner_with()
                with patch.object(bootstrap.shutil, "which", return_value="docker"):
                    with self.assertRaises(bootstrap.Refused) as raised:
                        bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner)
                self.assertEqual(raised.exception.code, "langfuse_login_required")
                self.assertEqual(runner.calls, [])

    def test_network_is_created_only_when_missing(self):
        run = runner_with(network_exists=False)
        bootstrap.ensure_network(run)
        self.assertEqual(run.calls[-1][:3], ["docker", "network", "create"])
        run = runner_with(network_exists=True)
        bootstrap.ensure_network(run)
        self.assertEqual(len(run.calls), 1)


if __name__ == "__main__":
    unittest.main()
