#!/usr/bin/env python3
"""Disposable gateway contract, without observability or application databases."""
import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from email.utils import parsedate_to_datetime
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap

ROOT = Path(__file__).resolve().parent.parent
PROJECT = os.environ.get("SMOKE_PROJECT", f"llm-gateway-smoke-access-{os.getpid()}")
if not PROJECT.startswith("llm-gateway-smoke-"):
    raise SystemExit("use a unique SMOKE_PROJECT beginning llm-gateway-smoke-")
HTTP_PORT = int(os.environ.get("SMOKE_HTTP_PORT", "18080"))
HTTPS_PORT = int(os.environ.get("SMOKE_HTTPS_PORT", "18443"))
NETWORK = PROJECT + "-access"
GATEWAY = NETWORK + "-gateway"
BACKEND = NETWORK + "-backend"
IMAGES = {}
service = ""
for line in (ROOT / "compose.yaml").read_text().splitlines():
    if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
        service = line.strip(": ")
    if line.strip().startswith("image: "):
        IMAGES[service] = line.split(":-", 1)[1].removesuffix("}")


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout


class AccessSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker("network", "create", NETWORK)
        cls.addClassCleanup(docker, "network", "rm", NETWORK)
        backend = """
import http.server, json, threading
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Strict-Transport-Security', 'max-age=1000')
        self.send_header('X-Smoke-Upstream', str(self.server.server_port))
        self.send_header('X-Smoke-Path', self.path)
        self.end_headers()
        self.wfile.write(json.dumps(dict(self.headers)).encode())
    do_POST = do_GET
    def log_message(self, *args):
        pass
for port in (3000, 9000, 9001):
    threading.Thread(target=http.server.HTTPServer(('', port), Handler).serve_forever, daemon=True).start()
http.server.HTTPServer(('', 4000), Handler).serve_forever()
"""
        docker("run", "-d", "--name", BACKEND, "--network", NETWORK,
               "--network-alias", "litellm", "--network-alias", "langfuse-web", "--network-alias", "rustfs",
               "--log-driver", "journald", "--log-opt", "cache-disabled=true",
               "--entrypoint", "python3", IMAGES["litellm"], "-u", "-c", backend)
        cls.addClassCleanup(docker, "rm", "-f", BACKEND)
        cls.subnet = json.loads(docker("network", "inspect", NETWORK))[0]["IPAM"]["Config"][0]["Subnet"]

    def setUp(self):
        self.gateway_started = False
        self.state = tempfile.TemporaryDirectory(prefix=PROJECT)
        self.addCleanup(self.state.cleanup)

    def tearDown(self):
        if self.gateway_started:
            docker("rm", "-f", GATEWAY)

    def start(self, mode="local", trust="", origins=None, operators=None):
        domain = "localhost" if mode == "local" else "gateway.test"
        settings = bootstrap.access_settings({"LG_ACCESS_MODE": mode, "LG_PUBLIC_DOMAIN": domain,
                                              "LG_TRUSTED_PROXIES": trust,
                                              "LG_PUBLIC_PORT_SUFFIX": f":{HTTPS_PORT}" if mode == "public" else "",
                                              **(origins or {})})
        settings.update(LG_HTTPS_PUBLISHED=str(mode != "proxy").lower(),
                        LG_OPERATOR_ALLOW=self.subnet if operators is None else operators)
        args = ["run", "-d", "--name", GATEWAY, "--network", NETWORK,
                "--log-driver", "journald", "--log-opt", "cache-disabled=true",
                "-p", f"127.0.0.1:{HTTP_PORT}:80", "--tmpfs", "/data", "--tmpfs", "/config"]
        if mode != "proxy":
            args += ["-p", f"127.0.0.1:{HTTPS_PORT}:443"]
        for key, value in settings.items():
            args += ["-e", f"{key}={value}"]
        args += ["-v", f"{self.state.name}:/srv/state:ro",
                 "-v", f"{ROOT / 'docker/caddy'}:/etc/caddy:ro",
                 "-v", f"{ROOT / 'docker/caddy/console'}:/srv/console:ro",
                 "--entrypoint", "/bin/sh", IMAGES["caddy"], "/etc/caddy/access-mode.sh",
                 "caddy", "run", "--config", "/etc/caddy/Caddyfile"]
        docker(*args)
        self.gateway_started = True
        for attempt in range(60):
            try:
                if self.request("/health/litellm", domain)[0] == 200:
                    if mode != "local" or self.request("/health/litellm", domain, tls=True)[0] == 200:
                        return
            except (OSError, subprocess.CalledProcessError):
                pass
            time.sleep(0.5)
        self.fail("gateway did not start; inspect the disposable container logs")

    def request(self, path, host="localhost", tls=False, headers=None, method="GET"):
        if tls:
            root = docker("exec", GATEWAY, "cat", "/data/caddy/pki/authorities/local/root.crt")
            context = ssl.create_default_context(cadata=root)
            connection = bootstrap.LocalHTTPSConnection(host, HTTPS_PORT, "127.0.0.1", context)
        else:
            connection = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
        try:
            connection.request(method, path, headers={"Host": host, **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_observer_caddy_probe_is_independent_of_public_routes(self):
        self.start()
        info = json.loads(docker("inspect", GATEWAY))[0]
        ip = info["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
        connection = http.client.HTTPConnection(ip, 8081, timeout=5)
        try:
            connection.request("GET", "/health/status")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b"ok"))
        finally:
            connection.close()

    def test_public_status_transport_and_frozen_evidence(self):
        frozen = {"schemaVersion": 1, "stack": "gateway", "generatedAt": "2026-09-20T12:00:00Z",
                  "configurationObservedAt": "2026-09-20T11:00:00Z",
                  "configurationValidForSeconds": 120, "telemetry": "unknown",
                  "components": [{"id": "postgres", "kind": "service", "configured": True,
                                  "state": "healthy", "observedAt": "2026-09-20T11:00:00Z",
                                  "validForSeconds": 120}]}
        path = Path(self.state.name) / "status.json"
        for mode, host in (("local", "localhost"), ("proxy", "gateway.test")):
            path.write_text(json.dumps(frozen))
            self.start(mode, self.subnet if mode == "proxy" else "", operators="127.0.0.0/8 ::1")
            for tls in ((False, True) if mode == "local" else (False,)):
                for method in ("GET", "HEAD"):
                    status, headers, body = self.request("/status.json", host, tls, method=method,
                        headers={"Authorization": "Bearer secret", "Cookie": "session=secret",
                                 "If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT",
                                 "If-None-Match": "*", "Range": "bytes=0-5"})
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], "application/json")
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertLess(abs(parsedate_to_datetime(headers["Date"]).timestamp() - time.time()), 5)
                    self.assertNotIn("X-Smoke-Upstream", headers)
                    self.assertNotIn("ETag", headers)
                    self.assertEqual(json.loads(body) if method == "GET" else body,
                                     frozen if method == "GET" else b"")
                status, headers, body = self.request("/status.json", host, tls, method="POST")
                self.assertEqual((status, body), (405, b""))
                self.assertEqual(headers["Allow"], "GET, HEAD")
            path.unlink()
            status, headers, body = self.request("/status.json", host)
            self.assertEqual((status, body), (404, b""))
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(headers["Cache-Control"], "no-store")
            docker("rm", "-f", GATEWAY)
            self.gateway_started = False

    def test_local_protocols_without_redirect_or_hsts(self):
        self.start()
        for tls in (False, True):
            for host, path in (("localhost", "/"), ("litellm.localhost", "/health/readiness")):
                status, headers, body = self.request(path, host, tls)
                self.assertEqual(status, 200)
                self.assertNotIn("Location", headers)
                self.assertNotIn("Strict-Transport-Security", headers)

    def test_public_redirect_and_health_exception(self):
        self.start("public")
        status, headers, body = self.request("/v1/models", "litellm.gateway.test")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], f"https://litellm.gateway.test:{HTTPS_PORT}/v1/models")
        self.assertEqual(self.request("/health/litellm", "gateway.test")[0], 200)
        self.assertEqual(self.request("/", "rustfs.gateway.test")[1]["Location"], f"https://rustfs.gateway.test:{HTTPS_PORT}/")
        self.assertEqual(self.request("/", "untrusted.test")[1]["Location"], f"https://gateway.test:{HTTPS_PORT}/")

    def test_public_disabled_rustfs_does_not_redirect(self):
        self.start("public", origins={"LG_RUSTFS_CONSOLE": "off"})
        for path in ("/", "/rustfs/console/"):
            status, headers, _ = self.request(path, "rustfs.gateway.test")
            self.assertEqual(status, 404)
            self.assertNotIn("Location", headers)
        self.assertEqual(self.request("/", "litellm.gateway.test")[0], 308)

    def test_proxy_forwarding_and_http_only(self):
        hostname = "darkforge.tail694fe2.ts.net"
        origins = {f"LG_{app}_URL": f"https://{hostname}:{port}" for app, port in (
            ("LITELLM", 8443), ("LANGFUSE", 8444), ("S3", 8445), ("CONSOLE", 8446), ("RUSTFS", 8449))}
        for trust, expected in (("192.0.2.0/24", "http"), (self.subnet, "https")):
            self.start("proxy", trust, origins=origins, operators="127.0.0.0/8 ::1")
            status, headers, body = self.request("/", "litellm.gateway.test",
                                                  headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["X-Forwarded-Proto"], expected)
            for port, upstream in ((8443, "4000"), (8444, "3000"), (8445, "9000")):
                authority = f"{hostname}:{port}"
                path = "/langfuse/media/image.png?X-Amz-SignedHeaders=host&X-Amz-Signature=unchanged"
                status, headers, body = self.request(path, authority, headers={"X-Forwarded-Proto": "https"})
                self.assertEqual(status, 200)
                self.assertEqual(headers["X-Smoke-Upstream"], upstream)
                self.assertEqual(headers["X-Smoke-Path"], path)
                self.assertEqual(json.loads(body)["Host"], authority)
                self.assertEqual(json.loads(body)["X-Forwarded-Proto"], expected)
            for port in (8446,):
                status, headers, body = self.request("/origins.json", f"{hostname}:{port}")
                self.assertEqual(status, 200)
                for app in ("console", "litellm", "langfuse", "s3"):
                    self.assertEqual(json.loads(body)[app], origins[f"LG_{app.upper()}_URL"])
                self.assertNotIn("X-Smoke-Upstream", headers)
            self.assertEqual(self.request("/origins.json", f"{hostname}:8447")[0], 404)
            for path in ("/ui/", "/openapi.json", "/metrics"):
                self.assertEqual(self.request(path, f"{hostname}:8443")[0], 404)
            self.assertEqual(self.request("/health/readiness", f"{hostname}:8443")[2], b"")
            self.assertEqual(self.request("/versions.json", f"{hostname}:8446")[0], 404)
            self.assertEqual(self.request("/", "rustfs.gateway.test")[0], 404)
            self.assertEqual(self.request("/", f"{hostname}:8449")[0], 404)
            ports = json.loads(docker("inspect", GATEWAY))[0]["HostConfig"]["PortBindings"]
            self.assertEqual(set(ports), {"80/tcp"})
            if trust != self.subnet:
                docker("rm", "-f", GATEWAY)
                self.gateway_started = False

    def test_proxy_ui_redirect_keeps_https_origin(self):
        origin = "https://darkforge.tail694fe2.ts.net:8443"
        self.start("proxy", self.subnet, origins={"LG_LITELLM_URL": origin, "LG_RUSTFS_URL": "https://darkforge.tail694fe2.ts.net:8449"})
        self.assertEqual(self.request("/rustfs/console/", "darkforge.tail694fe2.ts.net:8449")[1]["X-Smoke-Upstream"], "9001")
        status, headers, _ = self.request("/ui?view=models", "darkforge.tail694fe2.ts.net:8443")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], origin + "/ui/?view=models")
        self.assertEqual(self.request("/", "darkforge.tail694fe2.ts.net:8449", headers={"Accept": "text/html"})[1]["Location"], "/rustfs/console/")
        self.assertEqual(self.request("/", "darkforge.tail694fe2.ts.net:8449", headers={"Accept": "application/json"})[1]["X-Smoke-Upstream"], "9001")
        self.assertEqual(self.request("/", "darkforge.tail694fe2.ts.net:8449", method="POST")[1]["X-Smoke-Upstream"], "9001")

    def test_ip_root_and_configured_application_origins(self):
        self.start()
        for tls in (False, True):
            self.assertEqual(self.request("/", "127.0.0.1", tls)[0], 200)
        status, headers, body = self.request("/origins.json", "arbitrary.test")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "scheme": "http", "domain": "localhost", "port": "", "console": "http://localhost",
            "litellm": "http://litellm.localhost", "langfuse": "http://langfuse.localhost", "s3": "http://s3.localhost", "rustfs": "http://rustfs.localhost", "rustfsConsole": "on", "grafana": "http://grafana.localhost", "backplane": "http://backplane.localhost"})

    def test_access_logs_redact_credentials(self):
        self.start()
        self.request("/?X-Amz-Signature=secret-query", headers={
            "Authorization": "Bearer secret-authorization", "Cookie": "secret-cookie", "X-Api-Key": "secret-key"})
        records = []
        for attempt in range(20):
            result = subprocess.run(["docker", "logs", GATEWAY], check=True, capture_output=True, text=True)
            for secret in ("secret-query", "secret-authorization", "secret-cookie", "secret-key"):
                self.assertNotIn(secret, result.stdout + result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            if any(record.get("request", {}).get("uri") == "/?REDACTED" for record in records):
                break
            time.sleep(0.1)
        self.assertTrue(any(record.get("request", {}).get("uri") == "/?REDACTED" for record in records))
        self.assertTrue(result.stderr)


if __name__ == "__main__":
    unittest.main()
