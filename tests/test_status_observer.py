"""Public observations use fake Docker/probe answers; no Docker calls."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import status_io as io
import status_observer as observer
import status_probes as probes

AT = '2026-09-20T12:00:00Z'
OLD = '2026-08-01T12:00:00Z'
SECRET = 'password://private-host/private-path?token=private-token'


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.http = {}
        self.endpoint = "unix:///var/run/docker.sock"
        self.security = []
        self.driver = "bridge"
        self.config = {'name': 'selected-project', 'networks': {'default': {'name': 'selected_default'}},
                       'services': {name: {'image': 'private/repo:operator-secret-tag',
                                           'environment': {'PASSWORD': SECRET}}
                                    for name in (*observer.SERVICES, 'rustfs-init')}}
        self.config['services']['litellm']['image'] = 'private/repo:v1.101.0@sha256:' + 'a' * 64
        self.containers = {}
        for number, name in enumerate(self.config['services'], 1):
            self.containers[name] = {
                'Id': f'{number:064x}', 'Image': 'sha256:' + 'b' * 64,
                'Config': {'Labels': {'com.docker.compose.project': 'selected-project',
                                      'com.docker.compose.service': name,
                                      'com.docker.compose.oneoff': 'False'},
                           'Env': [SECRET], 'Healthcheck': {'Test': ['CMD', 'true']}},
                'State': {'Status': 'running', 'Paused': False, 'StartedAt': OLD,
                          'Health': {'Status': 'healthy', 'Log': [{'End': OLD, 'ExitCode': 0}]}},
                'NetworkSettings': {'Networks': {'selected_default': {'IPAddress': f'172.18.0.{number}'}}},
            }
        self.containers['rustfs-init']['State'].update(Status='exited', ExitCode=0, FinishedAt=OLD)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:3] == ['docker', 'context', 'inspect']:
            return json.dumps([{'Endpoints': {'docker': {'Host': self.endpoint}}}])
        if argv[:2] == ['docker', 'info']:
            return json.dumps(self.security)
        if argv[:3] == ['docker', 'network', 'inspect']:
            return json.dumps([{'Driver': self.driver, 'Scope': 'local'}])
        if 'config' in argv:
            if 'config' in self.fail:
                raise io.Unavailable(SECRET)
            return json.dumps(self.config)
        if argv[:2] == ['docker', 'ps']:
            if 'inventory' in self.fail:
                raise io.Unavailable(SECRET)
            return '\n'.join(doc['Id'] + ' ' + name + ' False' for name, doc in self.containers.items())
        if argv[:2] == ['docker', 'inspect']:
            name, doc = next((name, doc) for name, doc in self.containers.items() if doc['Id'] == argv[2])
            if 'inspect:' + name in self.fail:
                return '{broken'
            return json.dumps([doc])
        if argv[:2] == ['docker', 'exec']:
            name = next(name for name, doc in self.containers.items() if doc['Id'] == argv[2])
            if name in self.fail:
                raise io.Unavailable(SECRET)
            return {'postgres': '18.6 (Debian build)\n18.6 (Debian build)\n',
                    'valkey': 'PONG\n# Server\r\nvalkey_version:9.1.2\r\n',
                    'clickhouse': '26.8.6.5\n', 'litellm': '1.101.0\n',
                    'caddy': 'v2.11.4 h1:build\n', 'rustfs': 'rustfs 1.0.0\n'}[name]
        name = next(name for name, doc in self.containers.items()
                    if doc['NetworkSettings']['Networks']['selected_default']['IPAddress'] == argv[2])
        if name in self.fail:
            raise io.Unavailable(SECRET)
        body = {'caddy': 'ok', 'rustfs': '',
                'litellm': '{"db":"connected","version":"1.101.0"}',
                'langfuse-web': '{"status":"OK","version":"4.37.0"}',
                'langfuse-worker': '{"status":"ok"}',
                'postgres-exporter': 'pg_up 1\npostgres_exporter_build_info{version="0.19.0"} 1\n',
                'valkey-exporter': 'redis_up 1\nredis_exporter_build_info{version="1.80.1"} 1\n'}[name]
        return json.dumps(self.http.get(name, [200, body]))


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / '.env'
        self.fake = FakeRunner()

    def collect(self, at=AT):
        return observer.collect(self.root, self.env, self.fake, lambda: at)

    def rows(self, doc=None):
        return {row['id']: row for row in (doc or self.collect())['components']}

    def test_independent_observations_and_public_allowlist(self):
        doc = self.collect()
        rows = self.rows(doc)
        self.assertEqual(len(rows), 12)
        self.assertEqual(doc['telemetry'], 'unknown')
        self.assertEqual(doc['configurationObservedAt'], AT)
        for name in observer.SERVICES:
            self.assertEqual(rows[name]['state'], 'healthy', name)
            self.assertEqual(rows[name]['observedImageId'], 'sha256:' + 'b' * 64)
        self.assertNotIn('observedVersion', rows['langfuse-worker'])
        self.assertEqual(rows['postgres']['observedVersion'], '18.6')
        self.assertEqual(rows['litellm']['configuredDigest'], 'sha256:' + 'a' * 64)
        self.assertEqual(rows['caddy']['configuredVersion'], 'custom')
        public = json.dumps(doc)
        for forbidden in (SECRET, 'selected-project', 'private/repo', 'operator-secret-tag',
                          '172.18', 'Healthcheck', 'private-token', str(self.root)):
            self.assertNotIn(forbidden, public)
        allowed = {'id', 'kind', 'configured', 'state', 'observedAt', 'validForSeconds',
                   'lastExecutionAt', 'configuredVersion', 'configuredDigest', 'observedVersion', 'observedImageId'}
        self.assertTrue(all(set(row) <= allowed for row in rows.values()))
        self.assertLess(len(public.encode()), 65536)

    def test_one_failed_probe_cannot_borrow_web_health_or_old_docker_health(self):
        for service in observer.SERVICES:
            with self.subTest(service=service):
                self.fake.fail = {service}
                rows = self.rows()
                self.assertEqual(rows[service]['state'], 'unavailable')
                self.assertEqual(rows[service]['observedAt'], AT)
                self.assertNotIn('observedVersion', rows[service])
                for sibling in set(observer.SERVICES) - {service}:
                    self.assertEqual(rows[sibling]['state'], 'healthy', sibling)

    def test_inventory_failure_and_missing_configuration_do_not_prove_absence(self):
        saved = self.fake.containers
        self.fake.containers = {}
        self.assertEqual(self.rows()['litellm']['state'], 'unknown')
        self.fake.containers = saved
        self.fake.fail = {'inventory'}
        rows = self.rows()
        self.assertEqual(rows['litellm']['state'], 'unknown')
        self.assertTrue(rows['litellm']['configured'])
        self.fake.fail.clear()
        del self.fake.containers['postgres']
        del self.fake.config['services']['valkey']
        rows = self.rows()
        self.assertEqual(rows['postgres']['state'], 'absent')
        self.assertEqual(rows['postgres']['observedAt'], AT)
        self.assertEqual(rows['valkey']['state'], 'unknown')
        self.assertIsNone(rows['valkey']['configured'])

    def test_configuration_failure_publishes_no_renewed_facts(self):
        observer.observe(self.root, self.env, self.fake, lambda: OLD)
        self.fake.fail = {'config'}
        doc = observer.observe(self.root, self.env, self.fake, lambda: AT)
        self.assertIsNone(doc['configurationObservedAt'])
        self.assertEqual(doc['generatedAt'], AT)
        for row in doc['components']:
            self.assertIsNone(row['configured'])
            self.assertIsNone(row['observedAt'])
            self.assertEqual(row['state'], 'unknown')
            self.assertNotIn('configuredVersion', row)
            self.assertNotIn('observedImageId', row)

    def test_malformed_inspection_and_wrong_identity_isolate_the_component(self):
        for mutation in ('malformed', 'identity', 'image'):
            with self.subTest(mutation=mutation):
                self.fake = FakeRunner()
                if mutation == 'malformed':
                    self.fake.fail.add('inspect:postgres')
                elif mutation == 'identity':
                    self.fake.containers['postgres']['Config']['Labels']['com.docker.compose.project'] = 'other'
                else:
                    self.fake.containers['postgres']['Image'] = SECRET
                row = self.rows()['postgres']
                self.assertNotIn('observedImageId', row)
                self.assertEqual(row['state'], 'healthy' if mutation == 'image' else 'unknown')

    def test_tasks_preserve_execution_time_and_never_invent_an_execution(self):
        io.task_record(self.root, self.env, OLD, 'healthy')
        for at in (AT, '2026-09-21T12:00:00Z'):
            rows = self.rows(self.collect(at))
            for name in observer.TASKS:
                self.assertEqual(rows[name]['state'], 'healthy')
                self.assertEqual(rows[name]['lastExecutionAt'], OLD)
                self.assertEqual(rows[name]['observedAt'], at)
        self.fake.containers['rustfs-init']['State']['ExitCode'] = 1
        self.assertEqual(self.rows()['rustfs-init']['state'], 'unavailable')
        del self.fake.containers['rustfs-init']
        row = self.rows()['rustfs-init']
        self.assertEqual(row['state'], 'unknown')
        self.assertIsNone(row['lastExecutionAt'])
        io.task_record(self.root, self.root / 'other.env', OLD, 'healthy')
        self.assertEqual(self.rows()['bootstrap']['state'], 'unknown')

    def test_frozen_document_is_unchanged_when_served_later(self):
        observer.observe(self.root, self.env, self.fake, lambda: OLD)
        path = self.root / 'data/console/status.json'
        before = path.read_bytes()
        with patch.object(observer, 'now', return_value=AT):
            saved = json.loads(path.read_bytes())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(saved['configurationObservedAt'], OLD)
        for row in saved['components']:
            if row['state'] == 'healthy':
                age = (observer.datetime.fromisoformat(AT) - observer.datetime.fromisoformat(row['observedAt'])).total_seconds()
                self.assertGreater(age, row['validForSeconds'])

    def test_bad_or_unsupported_probe_payloads_and_versions(self):
        for response, state in (([200, '<html>OK</html>'], 'unavailable'),
                                ([200, '{"status":"not ready"}'], 'unavailable'),
                                ([404, SECRET], 'unknown'), ([503, SECRET], 'unavailable')):
            self.fake.http['langfuse-web'] = response
            self.assertEqual(self.rows()['langfuse-web']['state'], state)
        self.fake.http['langfuse-web'] = [200, json.dumps({'status': 'OK', 'version': SECRET})]
        self.assertNotIn('observedVersion', self.rows()['langfuse-web'])
        self.fake.http['valkey-exporter'] = [200, 'redis_up 0\n']
        self.assertEqual(self.rows()['valkey-exporter']['state'], 'unavailable')
        self.fake.http['postgres-exporter'] = [200, 'pg_up 1\npg_up{server="other"} 0\n']
        self.assertEqual(self.rows()['postgres-exporter']['state'], 'unavailable')

    def test_configuration_is_inspected_before_probes_and_assembly(self):
        moments = iter(['2026-09-20T11:59:30Z', '2026-09-20T11:59:31Z'] +
                       ['2026-09-20T11:59:32Z'] * 11 + [AT])
        doc = observer.collect(self.root, self.env, self.fake, lambda: next(moments))
        self.assertEqual(doc['configurationObservedAt'], '2026-09-20T11:59:30Z')
        self.assertEqual(doc['generatedAt'], AT)
        self.assertEqual(self.rows(doc)['postgres']['observedAt'], '2026-09-20T11:59:32Z')

    def test_stopped_or_paused_containers_and_duplicates_are_not_healthy(self):
        self.fake.containers['postgres']['State']['Status'] = 'exited'
        self.fake.containers['valkey']['State']['Paused'] = True
        self.assertEqual(self.rows()['postgres']['state'], 'unavailable')
        self.assertEqual(self.rows()['valkey']['state'], 'unknown')
        row = observer.observe_service('litellm', ['a', 'b'], self.fake.config, AT, 'selected_default', self.fake, lambda: AT)
        self.assertEqual(row['state'], 'unknown')

    def test_invalid_task_times_cannot_create_success(self):
        state = self.fake.containers['rustfs-init']['State']
        for value in ('0001-01-01T00:00:00Z', '2026-09-20T12:00:06Z', SECRET, None):
            state['StartedAt'] = value
            row = self.rows()['rustfs-init']
            self.assertEqual(row['state'], 'unknown')
            self.assertIsNone(row['lastExecutionAt'])
        state.update(StartedAt=AT, FinishedAt='2026-09-20T12:00:00.100Z')
        self.assertEqual(self.rows()['rustfs-init']['state'], 'healthy')
        state['FinishedAt'] = OLD
        self.assertEqual(self.rows()['rustfs-init']['state'], 'unknown')

    def test_missing_executable_and_oversized_probe_have_distinct_outcomes(self):
        def missing(argv, **kwargs):
            if argv[:2] == ['docker', 'exec'] and argv[2] == self.fake.containers['postgres']['Id']:
                raise io.Unsupported()
            return self.fake(argv, **kwargs)
        doc = observer.collect(self.root, self.env, missing, lambda: AT)
        self.assertEqual(self.rows(doc)['postgres']['state'], 'unknown')
        self.fake.http['langfuse-web'] = [200, 'x' * 524288]
        self.assertEqual(self.rows()['langfuse-web']['state'], 'unavailable')

    def test_malformed_or_oversized_configuration_cannot_leak_fields(self):
        for invalid in ('{"name":"p","name":"q","services":{}}', '[1]',
                        '{"name":null,"services":{}}', 'x' * (observer.LIMIT + 1)):
            def malformed(argv, **kwargs):
                return invalid if 'config' in argv else self.fake(argv, **kwargs)
            doc = observer.collect(self.root, self.env, malformed, lambda: AT)
            self.assertIsNone(doc['configurationObservedAt'])
            self.assertTrue(all(row['configured'] is None for row in doc['components']))

    def test_public_version_grammar_is_narrower_than_operator_tags(self):
        for service in observer.SERVICES:
            for value in (SECRET, 'customer-123', 'latest', '1.2.3-private', '1.2.3\n', '١.٢.٣', 'a' * 129):
                self.assertIsNone(probes.version(service, value))
        self.assertEqual(probes.version('postgres', '18.6-alpine3.22'), '18.6')


if __name__ == '__main__':
    unittest.main()

    def test_remote_rootless_or_nonbridge_context_never_dials_container_addresses(self):
        for field, value in (('endpoint', 'ssh://operator@remote'), ('security', ['name=rootless']), ('driver', 'overlay')):
            with self.subTest(field=field), patch.object(self.fake, field, value), \
                    patch.object(observer, 'probe') as probe:
                rows = {row['id']: row for row in self.collect()['components']}
                self.assertEqual(rows['litellm']['state'], 'unknown')
                self.assertEqual(rows['postgres']['state'], 'unknown')
                probe.assert_not_called()
