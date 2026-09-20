"""Fixed read-only service probes. Public claims and limits: operations/status.md."""

from http.client import HTTPConnection, HTTPException
import ipaddress
import re
import sys
from pathlib import Path

from status_io import Unavailable, Unsupported, read_json, run

# No arbitrary suffixes: operator labels can contain private installation details.
SEMVER = r'\d{1,4}\.\d{1,4}\.\d{1,4}'
PATTERNS = {
    'caddy': rf'v?({SEMVER})(?:-alpine)?',
    'litellm': rf'v?({SEMVER})(?:-stable)?',
    'langfuse-web': rf'v?({SEMVER})',
    'langfuse-worker': rf'v?({SEMVER})',
    'postgres': r'(\d{1,3}\.\d{1,3})(?:-(?:alpine(?:\d+\.\d+)?|bookworm|trixie))?',
    'clickhouse': r'(\d{1,4}\.\d{1,4}\.\d{1,4}\.\d{1,4})(?:-alpine)?',
    'valkey': rf'({SEMVER})(?:-alpine(?:\d+\.\d+)?)?',
    'rustfs': rf'v?({SEMVER})',
    'postgres-exporter': rf'v?({SEMVER})',
    'valkey-exporter': rf'v?({SEMVER})',
}


def version(service, value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(PATTERNS[service], value, re.ASCII)
    return match[1] if match else None


def configured_image(service, image):
    if not isinstance(image, str):
        return {}
    reference, _, digest = image.partition('@')
    tag = reference.rsplit('/', 1)[-1].partition(':')[2]
    result = {'configuredVersion': version(service, tag) or 'custom'} if tag else {}
    if re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        result['configuredDigest'] = digest
    return result


def http(ip, port, path, runner):
    # A separate bounded process gives the entire HTTP exchange a hard deadline,
    # including trickled headers/bodies. No proxies, redirects, or credentials.
    ipaddress.ip_address(ip)
    text = runner([sys.executable, str(Path(__file__).resolve()), ip, str(port), path],
                  timeout=4, limit=524288)
    response = read_json(text, 524288)
    if (not isinstance(response, list) or len(response) != 2
            or type(response[0]) is not int or not isinstance(response[1], str)):
        raise Unavailable()
    if response[0] in (401, 403, 404):
        return None
    if response[0] != 200:
        raise Unavailable()
    return response[1]


def execute(container, command, runner):
    # timeout also bounds the process inside the container if the Docker client dies.
    return runner(['docker', 'exec', container, 'timeout', '-s', 'KILL', '3', *command],
                  timeout=4, limit=65536)


def readiness(service, container, ip, runner):
    """Return only state and a parsed release; unsupported evidence stays unknown."""
    observed_version = None
    state = 'unknown'
    if service == 'postgres':
        # Read the server's version, not the client binary's. Both application DBs
        # must accept a bounded read-only query; no application rows are touched.
        command = ['sh', '-ec',
                   'export PGCONNECT_TIMEOUT=2 PGOPTIONS="-c statement_timeout=2000"; '
                   'psql -X -w -h /var/run/postgresql -p 5432 -U postgres -d langfuse -Atqc "SHOW server_version"; '
                   'psql -X -w -h /var/run/postgresql -p 5432 -U postgres -d litellm -Atqc "SHOW server_version"']
        lines = execute(container, command, runner).strip().splitlines()
        versions = [version(service, line.split(' ', 1)[0]) for line in lines]
        if len(versions) != 2 or not versions[0] or versions[0] != versions[1]:
            raise Unavailable()
        return 'healthy', versions[0]
    if service == 'clickhouse':
        text = execute(container, ['sh', '-ec',
                       'exec clickhouse-client --host 127.0.0.1 --max_execution_time 2 '
                       '--query "SELECT version()"'], runner)
        observed_version = version(service, text.strip())
        if not observed_version:
            raise Unavailable()
        return 'healthy', observed_version
    if service == 'valkey':
        text = execute(container, ['sh', '-ec',
                       'export VALKEYCLI_AUTH="$VALKEY_PASSWORD"; '
                       'valkey-cli -h 127.0.0.1 -p 6379 --raw PING; exec valkey-cli -h 127.0.0.1 -p 6379 --raw INFO server'], runner)
        match = re.search(r'^valkey_version:([^\r\n]+)\r?$', text, re.MULTILINE)
        if not text.startswith('PONG\n') or not match or not version(service, match[1]):
            raise Unavailable()
        return 'healthy', version(service, match[1])

    endpoints = {
        'caddy': (8081, '/health/status'),
        'litellm': (4000, '/health/readiness'),
        'langfuse-web': (3000, '/api/public/health?failIfDatabaseUnavailable=true'),
        'langfuse-worker': (3030, '/api/ready'),
        'rustfs': (9000, '/health/ready'),
        'postgres-exporter': (9187, '/metrics'),
        'valkey-exporter': (9121, '/metrics'),
    }
    if not ip:
        return state, observed_version
    body = http(ip, *endpoints[service], runner)
    if body is None:
        return state, observed_version
    if service == 'caddy':
        if body != 'ok':
            raise Unavailable()
        state = 'healthy'
    elif service == 'rustfs':
        state = 'healthy'
    elif service in ('litellm', 'langfuse-web', 'langfuse-worker'):
        data = read_json(body)
        if not isinstance(data, dict):
            raise Unavailable()
        if service == 'litellm' and data.get('db') != 'connected':
            raise Unavailable()
        expected = {'langfuse-web': 'OK', 'langfuse-worker': 'ok'}
        if service in expected and data.get('status') != expected[service]:
            raise Unavailable()
        observed_version = version(service, data.get('version'))
        if service == 'langfuse-web':
            ready = http(ip, 3000, '/api/public/ready', runner)
            if ready is None:
                return 'unknown', observed_version
        state = 'healthy'
    else:
        metric = 'pg_up' if service == 'postgres-exporter' else 'redis_up'
        samples = re.findall(r'^' + metric + r'(?:\{[^\n]*\})? ([^\n]+)$', body, re.MULTILINE)
        if not samples or any(value != '1' for value in samples):
            raise Unavailable()
        state = 'healthy'
        build = 'postgres_exporter_build_info' if service == 'postgres-exporter' else 'redis_exporter_build_info'
        match = re.search(r'^' + build + r'\{[^\n]*\bversion="([^"]+)"[^\n]*\} 1$', body, re.MULTILINE)
        observed_version = version(service, match[1]) if match else None

    return state, observed_version


def probe(service, container, ip, runner=run):
    try:
        state, observed_version = readiness(service, container, ip, runner)
    except Unsupported:
        state, observed_version = 'unknown', None
    except Unavailable:
        state, observed_version = 'unavailable', None
    commands = {
        'caddy': ['caddy', 'version'],
        'rustfs': ['rustfs', '--version'],
        'litellm': ['python3', '-c', 'import importlib.metadata; print(importlib.metadata.version("litellm"))'],
    }
    if service in commands and observed_version is None:
        try:
            text = execute(container, commands[service], runner).strip()
            if service == 'caddy':
                text = text.split(' ', 1)[0]
            elif service == 'rustfs':
                text = text.removeprefix('rustfs ')
            observed_version = version(service, text)
        except Unavailable:
            pass  # A missing version command does not undo a successful readiness probe.
    return state, observed_version


def http_main():
    import json
    connection = HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=3)
    try:
        connection.request('GET', sys.argv[3], headers={'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        body = response.read(262145)
        if len(body) > 262144:
            return 1
        print(json.dumps([response.status, body.decode('utf-8')]))
        return 0
    except (OSError, ValueError, HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(http_main())
