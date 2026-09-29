import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap


class ConsoleTests(unittest.TestCase):
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
for (const url of ['https://litellm.example.test', 'http://litellm.example.test/'])
  assert.equal(c.parseStatus({...valid, components: [{...component('litellm'), url}]})
    .components.has('litellm'), true, url);
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
  let result;
  const answers = {litellm: 'healthy', rustfs: 'healthy', 'rustfs-console': 'unreachable'};
  const probe = async (id) => { probed.push(id); return answers[id]; };
  const encoder = new TextEncoder();
  let cancelled = 0;
  const response = (body, {type = 'application/json', length, status = 200, fail} = {}) => {
    const bytes = encoder.encode(body);
    const headers = new Headers({'Content-Type': type});
    if (length !== undefined) headers.set('Content-Length', String(length));
    let offset = 0;
    return {
      status, headers,
      body: new ReadableStream({
        pull(controller) {
          if (fail) return controller.error(fail);
          if (offset === bytes.length) return controller.close();
          const end = Math.min(offset + Math.ceil(bytes.length / 2), bytes.length);
          controller.enqueue(bytes.slice(offset, end));
          offset = end;
        },
        cancel() { cancelled++; },
      }, {highWaterMark: 0}),
    };
  };
  const fakeFetch = (reply) => (path, options) => {
    assert.equal(path, '/status.json');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    assert.ok(options.signal instanceof AbortSignal);
    return reply(options);
  };
  const fromResponse = (body, options) => () => c.getStatus(fakeFetch(() => response(body, options)));
  const validBody = JSON.stringify(valid);
  const transportCases = [
    ['wrong type', fromResponse(validBody, {type: 'text/html'})],
    ['HTTP error', fromResponse(validBody, {status: 204})],
    ['declared oversize', fromResponse(validBody, {length: 65537})],
    ['streamed oversize', fromResponse(validBody + ' '.repeat(65537))],
    ['misleading length', fromResponse(validBody + ' '.repeat(65537), {length: 1})],
    ['invalid JSON', fromResponse('{')],
    ['invalid UTF-8', () => c.getStatus(fakeFetch(() => ({
      status: 200,
      headers: new Headers({'Content-Type': 'application/json'}),
      body: new ReadableStream({start(controller) { controller.enqueue(Uint8Array.of(255)); controller.close(); }}),
    })))],
    ['read failure', fromResponse(validBody, {fail: new Error('read failed')})],
    ['timeout', fromResponse(validBody, {fail: new DOMException('deadline', 'TimeoutError')})],
  ];
  for (const [name, getStatus] of transportCases) {
    const cancelledBefore = cancelled;
    probed = [];
    result = await c.load(getStatus, probe, cards);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []], name);
    if (name === 'declared oversize')
      assert.equal(cancelled, cancelledBefore + 1, 'declared oversized body is cancelled');
    result = await c.load(fromResponse(validBody, {type: 'Application/JSON; charset=utf-8'}), probe, cards);
    assert.ok(result.status, name + ' then valid');
    assert.deepEqual(result.health, answers, name + ' then valid');
    probed = [];
    result = await c.load(getStatus, probe, cards);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []], 'valid then ' + name);
  }
  const multibyte = JSON.stringify({...valid, components: valid.components.map((item) =>
    item.id === 'litellm' ? {...item, name: 'é'} : item)});
  const exact = multibyte + ' '.repeat(65536 - encoder.encode(multibyte).length);
  assert.equal(encoder.encode(exact).length, 65536);
  result = await c.load(fromResponse(exact, {length: 65536}), probe, cards);
  assert.ok(result.status, 'exact 65536-byte document');
  const overLimit = exact + ' ';
  assert.equal(encoder.encode(overLimit).length, 65537);
  assert.equal(overLimit.length, 65536);
  assert.deepEqual(JSON.parse(overLimit), JSON.parse(multibyte));
  for (const [name, options] of [['no length', {}], ['understated length', {length: 1}]]) {
    const cancelledBefore = cancelled;
    probed = [];
    result = await c.load(fromResponse(overLimit, options), probe, cards);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []], name);
    assert.equal(cancelled, cancelledBefore + 1, name + ' cancels oversized stream');
  }
  const originalTimeout = AbortSignal.timeout;
  const deadline = new AbortController();
  AbortSignal.timeout = (ms) => { assert.equal(ms, 4000); return deadline.signal; };
  try {
    probed = [];
    const waiting = c.load(() => c.getStatus(fakeFetch(({signal}) => ({
      status: 200,
      headers: new Headers({'Content-Type': 'application/json'}),
      body: new ReadableStream({start(controller) {
        controller.enqueue(encoder.encode(validBody.slice(0, 20)));
        signal.addEventListener('abort', () => controller.error(new DOMException('deadline', 'TimeoutError')));
      }}),
    }))), probe, cards);
    setTimeout(() => deadline.abort(), 0);
    result = await waiting;
    assert.equal(deadline.signal.aborted, true);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []], 'body deadline clears status');
  } finally {
    AbortSignal.timeout = originalTimeout;
  }
  result = await c.load(async () => valid, probe, cards);
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
    {...component('litellm'), url: 'https://litellm.example.test/console'},
    {...component('litellm'), url: 'https://litellm.example.test/%2f'},
    {...component('litellm'), url: 'https://user:pass@litellm.example.test/'},
    {...component('litellm'), url: 'https://litellm.example.test/?'},
    {...component('litellm'), url: 'https://litellm.example.test/#'},
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
  console.log('GATEWAY_CONSOLE_NODE_ASSERTIONS_COMPLETE');
})().catch((error) => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "docker/caddy/console/console.js")],
                                input=json.dumps(document), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GATEWAY_CONSOLE_NODE_ASSERTIONS_COMPLETE", result.stdout)
