"""Bootstrap contract. Docker is never called; a fake runner answers instead."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
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
        match = re.search(r"^  langfuse-web:\n\s+image: [^\s@]+:([^\s@]+)@", compose.read_text(), re.M)
        self.assertIsNotNone(match)
        self.assertEqual(doc["images"]["langfuse"], match[1])
        self.assertRegex(doc["images"]["langfuse"], r"^\d+\.\d+\.\d+$")
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

    def test_development_policy_opt_in_precedence_and_overlap(self):
        self.render()
        backups = self.root / "backups"
        backups.mkdir()
        data = self.root / "pg"
        base = self.env.read_text() + (f"\nLG_BACKUP_DIR={backups}\nLG_POSTGRES_DATA_DIR={data}\n"
                                      "LANGFUSE_INIT_USER_EMAIL=operator@gateway.test\n")
        key = "LG_ALLOW_SAME_FILESYSTEM_BACKUP"
        for saved, shell, target, error in (
            ("true", {}, data, None),
            ("false", {key: "true"}, data, None),
            ("true", {key: "false"}, data, "backup_dir_same_filesystem"),
            ("true", {key: ""}, data, "invalid_backup_policy"),
            ("TRUE", {}, data, "invalid_backup_policy"),
            ("true", {}, backups / "pg", "backup_dir_overlap"),
        ):
            with self.subTest(saved=saved, shell=shell, target=target):
                self.env.write_text(base + f"{key}={saved}\nLG_POSTGRES_DATA_DIR={target}\n")
                runner = runner_with()
                warning = io.StringIO()
                with patch.dict(os.environ, shell, clear=True), redirect_stderr(warning), \
                     patch.object(bootstrap.shutil, "which", return_value="docker"), \
                     patch.object(bootstrap, "write_versions"), patch.object(bootstrap, "probe_gateway"):
                    if error:
                        with self.assertRaises(bootstrap.Refused) as raised:
                            bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner)
                        self.assertEqual(raised.exception.code, error)
                        self.assertEqual(runner.calls, [])
                    else:
                        self.assertEqual(bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner), 0)
                        self.assertTrue(any("up" in call for call in runner.calls))
                        self.assertIn("disk loss affects both", warning.getvalue())
                        self.assertTrue(data.is_dir())

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

    def test_access_mode_defaults_and_conflicts(self):
        for mode, scheme, listener, issuer in (
            ("local", "http", "dual", "internal"),
            ("public", "https", "https", "acme"),
            ("proxy", "https", "http", "none"),
        ):
            with self.subTest(mode=mode):
                values = bootstrap.access_settings({"LG_ACCESS_MODE": mode, "LG_PUBLIC_DOMAIN": "gateway.test",
                                                    "LG_TRUSTED_PROXIES": "172.30.0.0/24"})
                self.assertEqual(tuple(values[key] for key in ("LG_SCHEME", "LG_LISTEN_SCHEME", "LG_TLS_ISSUER")),
                                 (scheme, listener, issuer))
                environment = {**values, "LG_HTTPS_PUBLISHED": str(mode != "proxy").lower()}
                result = subprocess.run(["sh", str(self.template.parent / "docker/caddy/access-mode.sh"), "true"],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bootstrap.access_settings({})["LG_ACCESS_MODE"], "local")
        for values in (
            {"LG_ACCESS_MODE": "invalid"},
            {"LG_SCHEME": "ftp"},
            {"LG_ACCESS_MODE": "public"},
            {"LG_ACCESS_MODE": "public", "LG_PUBLIC_DOMAIN": "gateway.test", "LG_SCHEME": "http"},
            {"LG_ACCESS_MODE": "proxy"},
            {"LG_ACCESS_MODE": "proxy", "LG_TRUSTED_PROXIES": "172.30.0.0/24", "COMPOSE_FILE": "compose.yaml"},
            {"LG_PUBLIC_DOMAIN": "127.0.0.1"},
            {"LG_PUBLIC_DOMAIN": 'untrusted"host'},
        ):
            with self.subTest(values=values), self.assertRaises(bootstrap.Refused):
                bootstrap.access_settings(values)

    def test_public_port_suffix_range_matches_gateway_entrypoint(self):
        for suffix, valid in (("", True), (":1", True), (":65535", True), (":000080", True),
                              (":0", False), (":0000", False), (":65536", False),
                              (":" + "9" * 5000, False), (":bad", False), (":", False)):
            with self.subTest(suffix=suffix[:20]):
                settings = {"LG_ACCESS_MODE": "local", "LG_PUBLIC_PORT_SUFFIX": suffix}
                if valid:
                    bootstrap.access_settings(settings)
                else:
                    with self.assertRaises(bootstrap.Refused) as raised:
                        bootstrap.access_settings(settings)
                    self.assertEqual(raised.exception.code, "invalid_access_settings")
                result = subprocess.run([
                    "sh", str(self.template.parent / "docker/caddy/access-mode.sh"), "true",
                ], env={**settings, "LG_SCHEME": "http", "LG_LISTEN_SCHEME": "dual",
                        "LG_TLS_ISSUER": "internal", "LG_PUBLIC_DOMAIN": "gateway.test"},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if valid else 1)

    def test_proxy_wildcard_binds_refused_before_bootstrap_or_gateway_start(self):
        for bind in ("0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "[0:0:0:0:0:0:0:0]",
                     "[0000::0]", "::0000", "::0.0.0.0", "[0:0:0:0:0:0:0.0.0.0]", "::ffff:0:0", "::ffff:0.0.0.0", "[::FFFF:0:0]",
                     "[0:0:0:0:0:ffff:0.0.0.0]", "0:0:0:0:0:ffff:0:0", "0000::FFFF:0000:0000",
                     "0:0::0:ffff:0:0", "0:0:0:0:0:ffff::", "0:0:0:0:0:ffff:0::",
                     "[0:0:0:0:0:ffff::0]"):
            with self.subTest(bind=bind):
                settings = {"LG_ACCESS_MODE": "proxy", "LG_BIND_HOST": bind,
                            "LG_TRUSTED_PROXIES": "172.30.0.0/24"}
                runner = runner_with()
                with patch.dict(os.environ, settings, clear=True), \
                     patch.object(bootstrap.shutil, "which", return_value="docker"), \
                     self.assertRaises(bootstrap.Refused) as raised:
                    bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner)
                self.assertEqual(raised.exception.code, "invalid_access_settings")
                self.assertIn("LG_BIND_HOST", raised.exception.detail)
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.env.exists())
                # Supply direct Compose's environment independently of bootstrap.
                result = subprocess.run([
                    "sh", str(self.template.parent / "docker/caddy/access-mode.sh"), "printf", "gateway-started",
                ], env={**settings, "LG_SCHEME": "https", "LG_LISTEN_SCHEME": "http",
                        "LG_TLS_ISSUER": "none", "LG_PUBLIC_DOMAIN": "gateway.test",
                        "LG_HTTPS_PUBLISHED": "false"}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn("LG_BIND_HOST", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_proxy_explicit_interfaces_and_standalone_wildcards_remain_supported(self):
        cases = [("proxy", bind) for bind in (None, "", "127.0.0.1", "192.0.2.10", "::1", "[::1]",
                                               "[2001:db8::10]", "::ffff:127.0.0.1", "[::ffff:c000:20a]",
                                               "ffff::", "0:ffff::")]
        cases += [(mode, bind) for mode in ("local", "public") for bind in ("0.0.0.0", "::", "[::]")]
        for mode, bind in cases:
            with self.subTest(mode=mode, bind=bind):
                settings = {"LG_ACCESS_MODE": mode, "LG_PUBLIC_DOMAIN": "gateway.test",
                            "LG_TRUSTED_PROXIES": "172.30.0.0/24" if mode == "proxy" else ""}
                if bind is not None:
                    settings["LG_BIND_HOST"] = bind
                values = bootstrap.access_settings(settings)
                self.assertEqual(values.get("LG_BIND_HOST"), bind)
                result = subprocess.run([
                    "sh", str(self.template.parent / "docker/caddy/access-mode.sh"), "printf", "gateway-started",
                ], env={**values, "LG_HTTPS_PUBLISHED": str(mode != "proxy").lower()},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "gateway-started")

    def test_gateway_probes_normalize_bracketed_ipv6_bind_addresses(self):
        for bind, address in (("[::1]", "::1"), ("[2001:db8::10]", "2001:db8::10"),
                              ("::1", "::1"), ("[::]", "::1"), ("::", "::1"),
                              ("0.0.0.0", "127.0.0.1"), ("127.0.0.1", "127.0.0.1")):
            with self.subTest(bind=bind), patch.object(bootstrap, "wait_ready") as wait, \
                 patch.object(bootstrap.ssl, "create_default_context"):
                bootstrap.probe_gateway({"LG_BIND_HOST": bind}, ["docker", "compose"], runner_with())
                urls = [bootstrap.urllib.parse.urlsplit(call.args[0]) for call in wait.call_args_list]
                self.assertEqual([url.hostname for url in urls], [address] * 4)
                self.assertEqual([url.port for url in urls], [80, 80, 443, 443])
                self.assertEqual([url.scheme for url in urls], ["http", "http", "https", "https"])
                self.assertTrue(all(call.kwargs["host"] == "localhost" for call in wait.call_args_list))

    def test_local_readiness_checks_both_protocols_with_own_ca(self):
        runner = runner_with()
        with patch.object(bootstrap, "wait_ready") as wait, \
             patch.object(bootstrap.ssl.SSLContext, "load_verify_locations") as trust:
            bootstrap.probe_gateway({"LG_ACCESS_MODE": "local", "LG_HTTP_PORT": "18080",
                                     "LG_HTTPS_PORT": "18443"}, ["docker", "compose"], runner)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [
            "http://127.0.0.1:18080/health/litellm", "http://127.0.0.1:18080/health/langfuse",
            "https://127.0.0.1:18443/health/litellm", "https://127.0.0.1:18443/health/langfuse",
        ])
        trust.assert_called_once()
        self.assertTrue(wait.call_args.kwargs["context"].check_hostname)


if __name__ == "__main__":
    unittest.main()
