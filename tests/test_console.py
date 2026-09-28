import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap


class ConsoleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for the static console check")
    def test_status_validation_and_refresh(self):
        available = {id: f"example/{id}:1.0" for id, *_ in bootstrap.COMPONENTS}
        selected = {id: image for id, image in available.items()
                    if id not in ("langfuse-web", "postgres-exporter", "valkey-exporter")}
        with tempfile.TemporaryDirectory() as directory:
            document = bootstrap.status_document(available, selected, {}, Path(directory),
                                                 "2026-09-28T00:00:00Z")
        script = r'''
const assert = require('node:assert/strict');
const c = require(process.argv[1]);
const valid = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const component = (id) => valid.components.find((item) => item.id === id);
assert.deepEqual([...c.parseStatus(valid).components.keys()], valid.components.map((item) => item.id));
assert.equal(component('langfuse-web').enabled, false);
assert.equal('url' in component('postgres'), false);
assert.deepEqual(Object.keys(valid.features), ['backups']);
assert.equal(valid.features.backups.lastCheckpointAt, null);
const withFeatures = {...valid, features: {
  backups: {configured: true, lastCheckpointAt: '2026-09-22T03:00:00Z'},
  alerts: {configured: false},
}};
assert.deepEqual(c.parseStatus(withFeatures).features, withFeatures.features);
assert.equal(c.backupsText(c.parseStatus(valid).features.backups), 'Configured · no checkpoint recorded');
assert.equal(c.backupsText(c.parseStatus(withFeatures).features.backups),
  'Configured · last checkpoint 2026-09-22 03:00 UTC');
assert.equal(c.backupsText(c.parseStatus({...valid, features: {
  backups: {configured: false, lastCheckpointAt: null}}}).features.backups), 'Not configured');
assert.equal(c.parseStatus({...valid, features: {}}).features.backups, undefined);
// A bad required component is discarded, while an unknown field anywhere rejects the whole document.
assert.equal(c.parseStatus({...valid, components: [...valid.components, {id: 'partial'}]})
  .components.has('partial'), false);
const invalid = [
  null, {...valid, contract: 3}, {...valid, stack: 'edge'}, {...valid, extra: true},
  (({configuredAt, ...rest}) => rest)(valid), (({features, ...rest}) => rest)(valid),
  {...valid, components: [{...component('litellm'), running: true}]},
  {...valid, components: [{id: 'partial', running: true}]},
  {...valid, features: {...valid.features, logs: {}}},
  {...valid, features: {backups: {...valid.features.backups, secret: true}}},
  {...valid, features: {alerts: {configured: true, destination: 'private'}}},
  {...valid, components: [...valid.components, {...component('litellm'), enabled: false}]},
  {...valid, components: Array.from({length: 33}, (_, i) => ({...component('litellm'), id: 'app-' + i}))},
];
for (const bad of invalid) assert.throws(() => c.parseStatus(bad), undefined, JSON.stringify(bad));
assert.deepEqual([
  c.summaryText(null, ['healthy']),
  c.summaryText(valid, []),
  c.summaryText(valid, ['healthy', 'unknown']),
  c.summaryText(valid, ['unknown']),
], ['Status unavailable', 'Nothing enabled', '1 of 1 reachable · 1 unknown', '1 unknown']);

(async () => {
  const cards = [
    {id: 'litellm', health: ['litellm']},
    {id: 'langfuse-web', health: ['langfuse-web']},
    {id: 'rustfs', health: ['rustfs', 'rustfs-console']},
  ];
  let probed = [];
  const answers = {litellm: 'healthy', rustfs: 'healthy', 'rustfs-console': 'unreachable'};
  const probe = async (id) => { probed.push(id); return answers[id]; };
  let result = await c.load(async () => valid, probe, cards);
  assert.deepEqual(probed, ['litellm', 'rustfs', 'rustfs-console']);
  assert.deepEqual(result.health, answers);
  assert.equal(c.appState(result.status.components.get('litellm'), [result.health.litellm]), 'healthy');
  assert.equal(c.appState(result.status.components.get('langfuse-web'), ['healthy']), 'disabled');
  assert.equal(c.appState(result.status.components.get('rustfs'),
    [result.health.rustfs, result.health['rustfs-console']]), 'degraded');
  for (const [features, key] of [
    [{backups: {configured: 'false', lastCheckpointAt: null}}, 'backups'],
    [{backups: {configured: true}}, 'backups'],
    [{backups: {configured: true, lastCheckpointAt: '2026-02-30T03:00:00Z'}}, 'backups'],
    [{backups: {configured: true, lastCheckpointAt: 'not a timestamp'}}, 'backups'],
    [{backups: null}, 'backups'],
    [{alerts: {configured: 'false'}}, 'alerts'],
    [{alerts: {}}, 'alerts'],
    [{alerts: null}, 'alerts'],
  ]) {
    result = await c.load(async () => valid, probe, cards);
    probed = [];
    result = await c.load(async () => ({...valid, features}), probe, cards);
    assert.ok(result.status, JSON.stringify(features));
    assert.equal(result.status.features[key], undefined, JSON.stringify(features));
    if (key === 'backups') assert.equal(c.backupsText(result.status.features.backups), 'Unknown');
    assert.equal(result.health.litellm, 'healthy', JSON.stringify(features));
    assert.equal(c.appState(result.status.components.get('litellm'), [result.health.litellm]), 'healthy');
    assert.deepEqual(probed, ['litellm', 'rustfs', 'rustfs-console'], JSON.stringify(features));
  }
  const omit = (record, key) => Object.fromEntries(Object.entries(record).filter(([field]) => field !== key));
  for (const bad of [
    omit(component('litellm'), 'name'),
    {...component('litellm'), name: ''},
    omit(component('litellm'), 'kind'),
    {...component('litellm'), kind: 'unknown'},
    omit(component('litellm'), 'image'),
    {...component('litellm'), image: 'example/litellm@sha256:digest'},
    omit(component('litellm'), 'version'),
    {...component('litellm'), version: 'invalid version'},
    omit(component('litellm'), 'health'),
    {...component('litellm'), health: '/health/postgres'},
    {...component('litellm'), url: 'javascript:alert(1)'},
  ]) {
    result = await c.load(async () => valid, probe, cards);
    assert.equal(c.appState(result.status.components.get('litellm'), [result.health.litellm]), 'healthy');
    probed = [];
    result = await c.load(async () => ({...valid, components: valid.components.map((item) =>
      item.id === 'litellm' ? bad : item)}), probe, cards);
    assert.equal(result.status.components.has('litellm'), false, JSON.stringify(bad));
    assert.equal(result.health.litellm, undefined, JSON.stringify(bad));
    assert.deepEqual(probed, ['rustfs', 'rustfs-console'], JSON.stringify(bad));
    assert.equal(c.appState(result.status.components.get('litellm'), ['healthy']), 'unknown');
  }
  // Invalid or absent metadata after a healthy refresh clears status and health and probes nothing.
  for (const next of [
    async () => ({...valid, extra: true}),
    ...[null, '2026-02-30T00:00:00Z', '2026-09-28', '2026-09-28T00:00:00+02:00']
      .map((configuredAt) => async () => ({...valid, configuredAt})),
    async () => ({...valid, components: [{...component('litellm'), running: true}]}),
    async () => ({...valid, features: {backups: {...valid.features.backups, secret: true}}}),
    async () => ({...valid, components: [...valid.components, component('litellm')]}),
    async () => { throw new Error('404'); },
  ]) {
    probed = [];
    result = await c.load(next, probe, cards);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []]);
    assert.equal(c.appState(result.status?.components.get('litellm'), [result.health.litellm ?? 'unknown']),
      'unknown');
    assert.equal(c.summaryText(result.status, []), 'Status unavailable');
  }
})().catch((error) => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "docker/caddy/console/console.js")],
                                input=json.dumps(document), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
