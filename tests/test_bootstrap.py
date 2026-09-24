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


CONTRACT_IPAM = '[{"Subnet":"172.30.0.0/24","IPRange":"172.30.0.128/25","Gateway":"172.30.0.1"}]'


def runner_with(volumes=(), network_exists=True, ipam=CONTRACT_IPAM, create_fails=False):
    calls = []

    def run(argv, **options):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "ls"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(volumes), "")
        if argv[:3] == ["docker", "network", "inspect"]:
            exists = network_exists or (create_fails and ["docker", "network", "create"] in [c[:3] for c in calls])
            return subprocess.CompletedProcess(argv, 0 if exists else 1, ipam if exists else "", "")
        if argv[:3] == ["docker", "network", "create"] and create_fails:
            return subprocess.CompletedProcess(argv, 1, "", "network with name platform already exists")
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

    def test_versions_json_uses_effective_compose_metadata_without_secrets(self):
        compose = Path(__file__).resolve().parent.parent / "compose.yaml"
        refs = {"langfuse-web": "mirror.test/langfuse:4.37.1@sha256:" + "a" * 64,
                "litellm": "local/gateway:vtrial", "postgres": "registry:5000/store@sha256:" + "b" * 64}
        command = ["docker", "compose", "--env-file", str(self.env)]
        calls = []
        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {
                name: {"image": ref, "environment": {"PASSWORD": "private-secret"}}
                for name, ref in refs.items()}}), "")
        with patch.dict(os.environ, {"COMPOSE_FILE": "compose.yaml:custom.yaml", "LG_LITELLM_IMAGE": "local/gateway:vtrial"}):
            resolved = bootstrap.images(command, runner)
        self.assertEqual(resolved, refs)
        self.assertEqual(calls, [command + ["config", "--format", "json"]])
        bootstrap.write_versions(self.root, compose, resolved)
        doc = json.loads((self.root / "data" / "console" / "versions.json").read_text())
        self.assertEqual(doc["images"]["langfuse"], "4.37.1")
        self.assertEqual(doc["images"]["litellm"], "vtrial")
        self.assertEqual(doc["images"]["postgres"], "sha256:" + "b" * 64)
        self.assertNotIn("private-secret", json.dumps(doc))
        with self.assertRaises(bootstrap.Refused) as raised:
            bootstrap.images(command, lambda argv: subprocess.CompletedProcess(argv, 1, "private-secret", "private-secret"))
        self.assertNotIn("private-secret", raised.exception.detail)

    def test_validation_isolates_exported_installation_settings(self):
        capture = self.root / "capture.json"
        docker = self.root / "docker"
        docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ['compose', 'version']:
    sys.exit(0)
Path(os.environ['TEST_CAPTURE']).write_text(json.dumps({key: value for key, value in os.environ.items()
    if key.startswith(('LG_', 'COMPOSE_')) or key == 'NEXTAUTH_SECRET'}))
sys.exit(19)
""")
        docker.chmod(0o755)
        shellcheck = self.root / "shellcheck"
        shellcheck.write_text("#!/bin/sh\nexit 0\n")
        shellcheck.chmod(0o755)
        dirty = {"LG_CADDY_IMAGE": "local:trial", "LG_POSTGRES_IMAGE": "local:trial",
                 "LG_ACCESS_MODE": "proxy", "COMPOSE_FILE": "/installation/custom.yaml",
                 "COMPOSE_PROFILES": "installed", "COMPOSE_ENV_FILES": "/installation/.env",
                 "NEXTAUTH_SECRET": "private-secret", "LG_BACKUP_DIR": "/installation/backups"}
        result = subprocess.run(["bash", str(self.template.parent / "scripts/validate.sh")],
                                env={**os.environ, **dirty, "PATH": str(self.root) + os.pathsep + os.environ['PATH'],
                                     "TEST_CAPTURE": str(capture)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 19)
        actual = json.loads(capture.read_text())
        for key in dirty:
            self.assertNotEqual(actual.get(key), dirty[key], key)
        self.assertEqual(actual['COMPOSE_FILE'], f"{self.template.parent}/compose.yaml:{self.template.parent}/compose.local.yaml")
        self.assertNotIn("private-secret", result.stdout + result.stderr)

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
                     patch.object(bootstrap, "write_status"), patch.object(bootstrap, "images", return_value={}), \
                     patch.object(bootstrap, "console_dir"), patch.object(bootstrap, "probe_gateway"):
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

    def installation(self):
        """A rendered env file with the settings a full bootstrap run requires."""
        self.render()
        backups = self.root / "backups"
        backups.mkdir()
        self.env.write_text(self.env.read_text() + f"\nLG_BACKUP_DIR={backups}\n"
                            f"LG_POSTGRES_DATA_DIR={self.root / 'pg'}\nLG_ALLOW_SAME_FILESYSTEM_BACKUP=true\n"
                            "LANGFUSE_INIT_USER_EMAIL=operator@gateway.test\n")
        return backups

    def compose_runner(self, selected):
        """Answer Compose config like a file where only `selected` services are outside profiles."""
        base = runner_with()
        services = {name: {"image": f"example/{name}:1.0@sha256:" + "c" * 64} for name, *_ in bootstrap.COMPONENTS}

        def run(argv, **options):
            if argv[-3:] == ["config", "--format", "json"]:
                base.calls.append(argv)
                every = argv[-5:-3] == ["--profile", "*"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {
                    name: service for name, service in services.items() if every or name in selected}}), "")
            return base(argv, **options)

        run.calls = base.calls
        return run

    def test_status_document_is_the_closed_v2_schema_from_compose_config(self):
        refs = {
            "caddy": "caddy:2.11.4@sha256:" + "a" * 64,
            "litellm": "ghcr.io/berriai/litellm:v1.101.0@sha256:" + "b" * 64,
            "langfuse-web": "registry:5000/langfuse/langfuse:4.37.0",
            "langfuse-worker": "langfuse/langfuse-worker:4garbage",
            "postgres": "mirror.test/postgres@sha256:" + "c" * 64,
            "clickhouse": "clickhouse/clickhouse-server:26.8.6.5",
            "valkey": "valkey/valkey:9.1.2", "rustfs": "rustfs/rustfs:1.0.0-alpha.93",
            "postgres-exporter": "prometheuscommunity/postgres-exporter:v0.19.0",
            "valkey-exporter": "oliver006/redis_exporter:v1.80.1",
            "rustfs-init": "amazon/aws-cli:2.32.0",
        }
        config = json.dumps({"services": {name: {"image": ref, "environment": {"PASSWORD": "private-secret"}}
                                          for name, ref in refs.items()}})
        available = bootstrap.images(["docker", "compose"], lambda argv: subprocess.CompletedProcess(argv, 0, config, ""))
        backups = self.root / "backups"
        for stamp, at in (("20260921T030000000000Z", "2026-09-21T03:00:00.5+00:00"),
                          ("20260922T030000000000Z", "2026-09-22T05:00:00+02:00")):
            (backups / stamp).mkdir(parents=True)
            (backups / stamp / "manifest.json").write_text(json.dumps({"timestamp": at}))
        (backups / "20260923T030000000000Z").mkdir()  # incomplete: no manifest
        (backups / "20260924T030000000000Z").mkdir()
        (backups / "20260924T030000000000Z" / "manifest.json").write_text("{")  # unreadable
        settings = bootstrap.access_settings({"LG_PUBLIC_DOMAIN": "gateway.test"})
        doc = bootstrap.status_document(available, available, settings, backups, "2026-09-23T16:00:00Z")
        text = json.dumps(doc)
        self.assertNotIn("private-secret", text)
        self.assertNotIn("sha256", text)
        self.assertEqual(set(doc), {"contract", "stack", "configuredAt", "components", "features"})
        self.assertEqual((doc["contract"], doc["stack"], doc["configuredAt"]), (2, "gateway", "2026-09-23T16:00:00Z"))
        self.assertEqual(doc["features"], {"backups": {"configured": True, "lastCheckpointAt": "2026-09-22T03:00:00Z"}})
        self.assertEqual([c["id"] for c in doc["components"]],
                         ["caddy", "litellm", "langfuse-web", "langfuse-worker", "postgres", "clickhouse",
                          "valkey", "rustfs", "postgres-exporter", "valkey-exporter"])
        fields = {"id", "name", "kind", "enabled", "image", "version", "health"}
        for component in doc["components"]:
            with self.subTest(component=component["id"]):
                self.assertTrue(fields <= set(component) <= fields | {"url"})
                self.assertRegex(component["id"], r"^[a-z][a-z0-9-]{0,31}$")
                self.assertIn(component["kind"], ("app", "datastore", "gateway", "collector", "runtime"))
                self.assertIs(component["enabled"], True)
                self.assertEqual(component["health"], "/health/" + component["id"])
                self.assertEqual(component["image"], refs[component["id"]].split("@")[0])
        by_id = {c["id"]: c for c in doc["components"]}
        self.assertEqual({key: c["version"] for key, c in by_id.items()}, {
            "caddy": "2.11.4", "litellm": "v1.101.0", "langfuse-web": "4.37.0", "langfuse-worker": None,
            "postgres": None, "clickhouse": "26.8.6.5", "valkey": "9.1.2", "rustfs": "1.0.0-alpha.93",
            "postgres-exporter": "v0.19.0", "valkey-exporter": "v1.80.1"})
        self.assertEqual({key: c["url"] for key, c in by_id.items() if "url" in c}, {
            "litellm": "http://litellm.gateway.test", "langfuse-web": "http://langfuse.gateway.test",
            "rustfs": "http://s3.gateway.test"})
        self.assertIsNone(bootstrap.status_document({}, {}, settings, self.root / "absent", "x")
                          ["features"]["backups"]["lastCheckpointAt"])

    def test_status_marks_services_outside_selected_profiles_disabled(self):
        self.installation()
        written = []
        runner = self.compose_runner(selected={"caddy", "litellm", "langfuse-web"})
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()), \
             patch.object(bootstrap.shutil, "which", return_value="docker"), \
             patch.object(bootstrap, "probe_gateway"), patch.object(bootstrap, "console_dir"), \
             patch.object(bootstrap, "write_status", side_effect=lambda root, doc: written.append(doc)):
            self.assertEqual(bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner), 0)
        configs = [call for call in runner.calls if call[-3:] == ["config", "--format", "json"]]
        self.assertEqual([call[-5:-3] == ["--profile", "*"] for call in configs], [True, False])
        self.assertEqual({c["id"]: c["enabled"] for c in written[0]["components"]},
                         {name: name in ("caddy", "litellm", "langfuse-web") for name, *_ in bootstrap.COMPONENTS})

    def test_status_is_written_atomically_and_only_after_readiness(self):
        self.installation()
        public = self.root / "data" / "console" / "status.json"
        console_dir = bootstrap.console_dir

        def ready(*args):
            self.assertFalse(public.exists(), "status published before readiness")
            self.assertTrue(any("up" in call for call in runner.calls))

        for failure in (bootstrap.Refused("not_ready", "gateway"), None):
            runner = self.compose_runner(selected={name for name, *_ in bootstrap.COMPONENTS})
            made = []

            def console(root):
                made.append(len(runner.calls))
                return console_dir(self.root)

            with self.subTest(failure=failure), patch.dict(os.environ, {}, clear=True), \
                 redirect_stderr(io.StringIO()), patch.object(bootstrap.shutil, "which", return_value="docker"), \
                 patch.object(bootstrap, "console_dir", side_effect=console), \
                 patch.object(bootstrap, "probe_gateway", side_effect=failure or ready):
                if failure:
                    with self.assertRaises(bootstrap.Refused):
                        bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner)
                    self.assertFalse(public.exists())
                else:
                    self.assertEqual(bootstrap.bootstrap(["--env-file", str(self.env)], runner=runner), 0)
                # The mount exists before Compose starts, or Docker creates it as root.
                self.assertLess(made[0], next(i for i, call in enumerate(runner.calls) if "up" in call))
        document = json.loads(public.read_text())
        self.assertEqual(document["contract"], 2)
        self.assertEqual(public.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in public.parent.iterdir()), ["status.json"])
        with patch.object(bootstrap.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            bootstrap.write_status(self.root, {"contract": 2, "partial": True})
        self.assertEqual(json.loads(public.read_text()), document)

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

    def test_missing_network_is_created_with_the_configured_allocation(self):
        run = runner_with(network_exists=False)
        bootstrap.ensure_network(run, "platform", *bootstrap.platform_allocation(
            {"LG_PLATFORM_SUBNET": "10.40.0.0/24", "LG_PLATFORM_IP_RANGE": "10.40.0.128/25"}))
        self.assertEqual(run.calls[-1], ["docker", "network", "create", "--driver", "bridge",
                                         "--subnet", "10.40.0.0/24", "--ip-range", "10.40.0.128/25",
                                         "--gateway", "10.40.0.1", "platform"])
        for values in ({"LG_PLATFORM_SUBNET": "172.30.0.5/24"}, {"LG_PLATFORM_IP_RANGE": "10.0.0.0/25"},
                       {"LG_PLATFORM_IP_RANGE": "172.30.0.0/25", "LG_TRUSTED_PROXIES": "172.30.0.2/32"}):
            with self.subTest(values=values), self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.platform_allocation(values)
            self.assertEqual(raised.exception.code, "invalid_platform_network")

    def test_existing_network_with_the_contract_allocation_is_used(self):
        run = runner_with()
        bootstrap.ensure_network(run, "platform", *bootstrap.platform_allocation({}))
        self.assertEqual(len(run.calls), 1)

    def test_existing_network_with_another_allocation_is_refused_with_both_values(self):
        second = '{"Subnet":"10.9.0.0/24"}'
        for ipam, observed in (('[{"Subnet":"172.18.0.0/16","Gateway":"172.18.0.1"}]', "subnet 172.18.0.0/16 ip-range none"),
                               ("null", "no IPAM configuration"),
                               (CONTRACT_IPAM[:-1] + "," + second + "]", "subnet 10.9.0.0/24 ip-range none")):
            with self.subTest(ipam=ipam), self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.ensure_network(runner_with(ipam=ipam))
            self.assertEqual(raised.exception.code, "platform_network_mismatch")
            self.assertIn(observed, raised.exception.detail)
            self.assertIn("expected subnet 172.30.0.0/24 ip-range 172.30.0.128/25", raised.exception.detail)
            self.assertIn("docker network rm platform", raised.exception.detail)

    def test_concurrent_creation_validates_the_winning_network(self):
        run = runner_with(network_exists=False, create_fails=True)
        bootstrap.ensure_network(run)
        self.assertEqual([call[:3] for call in run.calls], [["docker", "network", "inspect"],
                                                          ["docker", "network", "create"],
                                                          ["docker", "network", "inspect"]])
        with self.assertRaises(bootstrap.Refused) as raised:
            bootstrap.ensure_network(runner_with(network_exists=False, create_fails=True, ipam="[]"))
        self.assertEqual(raised.exception.code, "platform_network_mismatch")

    def test_access_mode_defaults_and_conflicts(self):
        for mode, scheme, listener, issuer in (
            ("local", "http", "dual", "internal"),
            ("public", "https", "https", "acme"),
            ("proxy", "https", "http", ""),
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
        for proxies in (None, ""):
            values = {"LG_ACCESS_MODE": "proxy", "COMPOSE_FILE": "compose.yaml:compose.proxy.yaml"}
            if proxies is not None:
                values["LG_TRUSTED_PROXIES"] = proxies
            self.assertEqual(bootstrap.access_settings(values)["LG_TRUSTED_PROXIES"], "172.30.0.2/32")
        for values in (
            {"LG_ACCESS_MODE": "invalid"},
            {"LG_SCHEME": "ftp"},
            {"LG_ACCESS_MODE": "public"},
            {"LG_ACCESS_MODE": "public", "LG_PUBLIC_DOMAIN": "gateway.test", "LG_SCHEME": "http"},
            {"LG_ACCESS_MODE": "proxy", "LG_TRUSTED_PROXIES": "172.30.0.0/24", "COMPOSE_FILE": "compose.yaml"},
            {"LG_PUBLIC_DOMAIN": "127.0.0.1"},
            {"LG_PUBLIC_DOMAIN": 'untrusted"host'},
        ):
            with self.subTest(values=values), self.assertRaises(bootstrap.Refused):
                bootstrap.access_settings(values)

    def test_public_port_suffix_range_matches_gateway_entrypoint(self):
        for suffix, valid in (("", True), (":1", True), (":65535", True), (":80", True),
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
        def runner(argv, **options):
            return subprocess.CompletedProcess(argv, 0, "internal root" if argv[-1].endswith("root.crt") else "", "")
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


    def test_default_issuer_per_mode_and_refused_pairs(self):
        self.assertEqual([bootstrap.tls_issuer(mode, "") for mode in ("local", "public", "proxy")], ["internal", "acme", ""])
        self.assertEqual(bootstrap.tls_issuer("proxy", "files"), "")
        base = {"LG_PUBLIC_DOMAIN": "gateway.test", "LG_TRUSTED_PROXIES": "172.30.0.2/32"}
        for mode, issuer, effective in (("local", "files", "files"), ("public", "files", "files"),
                                         ("proxy", "acme", ""), ("proxy", "none", "")):
            with self.subTest(mode=mode, issuer=issuer):
                values = bootstrap.access_settings({**base, "LG_ACCESS_MODE": mode, "LG_TLS_ISSUER": issuer})
                self.assertEqual(values["LG_TLS_ISSUER"], effective)
        for mode, issuer in (("public", "internal"), ("local", "acme"), ("local", "none"), ("public", "self-signed")):
            with self.subTest(mode=mode, issuer=issuer), self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.access_settings({**base, "LG_ACCESS_MODE": mode, "LG_TLS_ISSUER": issuer})
            self.assertEqual(raised.exception.code, "invalid_settings")

    def tls_settings(self, **values):
        return {"LG_ACCESS_MODE": "public", "LG_PUBLIC_DOMAIN": "gateway.test", "LG_TLS_ISSUER": "files",
                "LG_TLS_DIR": str(self.root / "certs"), **values}

    def test_files_issuer_without_key_is_refused(self):
        (self.root / "certs").mkdir()
        (self.root / "certs" / "tls.crt").write_text("certificate")
        runner = runner_with()
        for settings in (self.tls_settings(LG_TLS_DIR=""), self.tls_settings()):
            with self.subTest(directory=settings["LG_TLS_DIR"]), self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.check_tls_inputs(runner, settings, self.root)
            self.assertEqual(raised.exception.code, "invalid_settings")
            self.assertIn("LG_TLS_DIR", raised.exception.detail)
        self.assertEqual(runner.calls, [])

    def test_certificate_must_cover_every_configured_hostname(self):
        (self.root / "certs").mkdir()
        for name in ("tls.crt", "tls.key"):
            (self.root / "certs" / name).write_text(name)
        def openssl(names):
            return lambda argv, **options: subprocess.CompletedProcess(
                argv, 0, "X509v3 Subject Alternative Name: \n    " + ", ".join("DNS:" + n for n in names) + "\n", "")
        with patch.object(bootstrap.shutil, "which", return_value="/usr/bin/openssl"):
            bootstrap.check_tls_inputs(openssl(["gateway.test", "*.GATEWAY.test"]), self.tls_settings(), self.root)
            bootstrap.check_tls_inputs(openssl(["gateway.test", "litellm.gateway.test", "langfuse.gateway.test",
                                                "s3.gateway.test"]),
                                       self.tls_settings(LG_RUSTFS_CONSOLE="off"), self.root)
            for names, uncovered in ((["*.gateway.test"], "cover gateway.test;"),
                                     (["gateway.test", "*.litellm.gateway.test"], "litellm.gateway.test"),
                                     (["gateway.test", "litellm.gateway.test", "langfuse.gateway.test", "s3.gateway.test"],
                                      "rustfs.gateway.test")):
                with self.subTest(names=names), self.assertRaises(bootstrap.Refused) as raised:
                    bootstrap.check_tls_inputs(openssl(names), self.tls_settings(), self.root)
                self.assertEqual(raised.exception.code, "invalid_settings")
                self.assertIn(uncovered, raised.exception.detail)

    def test_acme_inputs(self):
        runner = runner_with()
        ca = "https://ca.example.internal/acme/acme/directory"
        bootstrap.check_tls_inputs(runner, self.tls_settings(LG_TLS_ISSUER="acme", LG_ACME_CA=ca, LG_ACME_EMAIL=""), self.root)
        bootstrap.check_tls_inputs(runner, self.tls_settings(LG_TLS_ISSUER="acme", LG_ACME_CA=ca,
                                                              LG_ACME_EAB_KEY_ID="kid", LG_ACME_EAB_HMAC="mac"), self.root)
        (self.root / "not-pem.crt").write_text("not a certificate")
        for values, setting in (({"LG_ACME_CA": "http://ca.example.internal/directory"}, "LG_ACME_CA"),
                                ({"LG_ACME_CA": ca, "LG_ACME_EAB_KEY_ID": "kid"}, "LG_ACME_EAB_HMAC"),
                                ({"LG_ACME_CA": ca, "LG_ACME_EAB_HMAC": "secret-mac"}, "LG_ACME_EAB_KEY_ID"),
                                ({"LG_ACME_CA_ROOT": str(self.root / "not-pem.crt")}, "LG_ACME_CA_ROOT needs LG_ACME_CA"),
                                ({"LG_ACME_CA": ca, "LG_ACME_CA_ROOT": str(self.root / "not-pem.crt")}, "PEM"),
                                ({"LG_TLS_CA": str(self.root)}, "LG_TLS_CA")):
            with self.subTest(values=values), self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.check_tls_inputs(runner, self.tls_settings(LG_TLS_ISSUER="acme", **values), self.root)
            self.assertEqual(raised.exception.code, "invalid_settings")
            self.assertIn(setting, raised.exception.detail)
            self.assertNotIn("secret-mac", raised.exception.detail)
        self.assertEqual(runner.calls, [])

    def test_compose_file_selection_strips_and_appends_managed_overlays(self):
        for values, expected in (
            ({"LG_ACCESS_MODE": "local", "LG_TLS_ISSUER": "files"},
             "compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml:compose.files.yaml"),
            ({"LG_ACCESS_MODE": "public", "LG_TLS_ISSUER": "acme", "LG_ACME_CA_ROOT": "ca.pem", "LG_ACME_EAB_KEY_ID": "kid",
              "COMPOSE_FILE": "compose.yaml:compose.public.yaml:compose.files.yaml:no-logs.yaml"},
             "compose.yaml:compose.public.yaml:compose.acme-ca-root.yaml:compose.acme-eab.yaml:no-logs.yaml"),
            ({"LG_ACCESS_MODE": "local", "LG_TLS_ISSUER": "files", "COMPOSE_FILE": "/srv/gw/compose.yaml"},
             "/srv/gw/compose.yaml:/srv/gw/compose.files.yaml"),
            ({"LG_ACCESS_MODE": "local", "LG_TLS_ISSUER": "files",
              "COMPOSE_FILE": "compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml:operator/no-logs.yaml"},
             "compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml:compose.files.yaml:operator/no-logs.yaml"),
            ({"LG_ACCESS_MODE": "proxy", "LG_TLS_ISSUER": "", "LG_ACME_CA_ROOT": "ca.pem",
              "COMPOSE_FILE": "compose.yaml:compose.proxy.yaml:compose.acme-eab.yaml"},
             "compose.yaml:compose.proxy.yaml"),
        ):
            with self.subTest(values=values):
                self.assertEqual(bootstrap.compose_files(values), expected)
        # The pre-start readability check expands the recorded mode token before splitting.
        runner = runner_with()
        bootstrap.check_tls_files_readable(runner, {"LG_ACCESS_MODE": "public", "LG_TLS_ISSUER": "files"},
                                           self.root, ["docker", "compose"])
        selected = runner.calls[0][1].removeprefix("COMPOSE_FILE=").split(os.pathsep)
        self.assertEqual(selected[:3], [str(self.root / name) for name in
                                        ("compose.yaml", "compose.public.yaml", "compose.files.yaml")])
        self.assertEqual(len(selected), 4)
        self.assertEqual(runner.calls[0][-3:], ["caddy", "-ec", "cat /certs/tls.crt /certs/tls.key >/dev/null"])
        # A recorded overlay from an earlier issuer is dropped by appending the selection.
        stale = "COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml:compose.files.yaml"
        self.env.write_text(self.template.read_text().replace(
            "COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml", stale))
        with patch.dict(os.environ, {"LG_TLS_ISSUER": ""}):
            self.render()
        lines = [line for line in self.env.read_text().splitlines() if line.startswith("COMPOSE_FILE=")]
        self.assertEqual(lines, [stale, "COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml"])
        self.render()
        self.assertEqual(self.env.read_text().count("\nCOMPOSE_FILE="), 2)

    def test_metrics_setting_records_the_compose_profile(self):
        def profiles():
            return [line for line in self.env.read_text().splitlines() if line.startswith("COMPOSE_PROFILES=")]
        self.env.write_text(self.template.read_text().replace("LG_METRICS=false", "LG_METRICS=true"))
        self.render()
        self.assertEqual(profiles(), ["COMPOSE_PROFILES=", "COMPOSE_PROFILES=metrics"])
        self.render()
        self.assertEqual(len(profiles()), 2)
        # Operator profiles survive; metrics follows the setting in either direction.
        self.env.write_text(self.env.read_text() + "COMPOSE_PROFILES=debug,metrics\nLG_METRICS=false\n")
        self.render()
        self.assertEqual(profiles()[-1], "COMPOSE_PROFILES=debug")
        # A shell COMPOSE_PROFILES applies to this run only, as Compose would read it.
        with patch.dict(os.environ, {"COMPOSE_PROFILES": "trial"}):
            self.render()
            self.assertEqual(os.environ["COMPOSE_PROFILES"], "trial")
        self.assertEqual(profiles()[-1], "COMPOSE_PROFILES=debug")
        # A shell LG_METRICS is saved with the recorded profiles, never the shell's; the next plain run keeps it.
        for shell in ({"LG_METRICS": "TRUE"}, {"LG_METRICS": "TRUE", "COMPOSE_PROFILES": "trial"}):
            self.env.write_text(self.env.read_text() + "LG_METRICS=false\nCOMPOSE_PROFILES=debug\n")
            with patch.dict(os.environ, shell):
                self.render()
                if "COMPOSE_PROFILES" in shell:
                    self.assertEqual(os.environ["COMPOSE_PROFILES"], "trial,metrics")
            self.render()
            self.assertEqual(profiles()[-1], "COMPOSE_PROFILES=debug,metrics")
            self.assertEqual(re.findall(r"^LG_METRICS=.*$", self.env.read_text(), re.M)[-1], "LG_METRICS=true")
        with patch.dict(os.environ, {"LG_METRICS": "yes"}), self.assertRaises(bootstrap.Refused) as raised:
            self.render()
        self.assertEqual(raised.exception.code, "invalid_settings")

    def test_probe_trust_order_and_hint(self):
        tls_ca, acme_root = self.root / "tls-ca.pem", self.root / "acme-root.pem"
        tls_ca.write_text("tls ca")
        acme_root.write_text("acme root")
        runner = lambda argv, **options: subprocess.CompletedProcess(argv, 0, "internal root", "")
        for issuer, values, expected in (
            ("internal", {"LG_TLS_CA": str(tls_ca)}, "internal root"),
            ("files", {"LG_TLS_CA": str(tls_ca), "LG_ACME_CA_ROOT": str(acme_root)}, "tls ca"),
            ("acme", {"LG_TLS_CA": str(tls_ca), "LG_ACME_CA_ROOT": str(acme_root)}, "tls ca"),
            ("acme", {"LG_ACME_CA_ROOT": str(acme_root)}, "acme root"),
            ("files", {"LG_ACME_CA_ROOT": str(acme_root)}, ""),
            ("acme", {}, ""),
        ):
            with self.subTest(issuer=issuer, values=values):
                self.assertEqual(bootstrap.probe_trust({"LG_TLS_ISSUER": issuer, **values}, runner, ["docker", "compose"]),
                                 expected)
        failure = bootstrap.Refused("not_ready", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        with patch.object(bootstrap, "wait_ready", side_effect=failure), self.assertRaises(bootstrap.Refused) as raised:
            bootstrap.probe_gateway({"LG_ACCESS_MODE": "public", "LG_PUBLIC_DOMAIN": "gateway.test"}, ["docker", "compose"],
                                    runner)
        self.assertIn("set LG_TLS_CA", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
