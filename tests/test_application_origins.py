"""Application origin contracts without Docker or application databases."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap

ORIGINS = {f'LG_{app}_URL': f'https://darkforge.tail694fe2.ts.net:{port}'
           for app, port in (('LITELLM', 8443), ('LANGFUSE', 8444), ('S3', 8445), ('CONSOLE', 8446), ('RUSTFS', 8449))}


class ApplicationOriginsTests(unittest.TestCase):
    def test_origin_derivation_and_conflicts(self):
        base = {'LG_ACCESS_MODE': 'proxy', 'LG_TRUSTED_PROXIES': '192.0.2.1/32'}
        for mode, scheme in (('local', 'http'), ('public', 'https'), ('proxy', 'https')):
            values = bootstrap.access_settings({**base, 'LG_ACCESS_MODE': mode,
                                                'LG_PUBLIC_DOMAIN': 'gateway.test',
                                                'LG_PUBLIC_PORT_SUFFIX': ':8448',
                                                'LG_LITELLM_URL': ''})
            self.assertEqual(values['LG_LITELLM_URL'], f'{scheme}://litellm.gateway.test:8448')
            self.assertEqual(values['LG_CONSOLE_URL'], f'{scheme}://gateway.test:8448')
        values = bootstrap.access_settings({**base, **ORIGINS})
        self.assertEqual({k: values[k] for k in ORIGINS}, ORIGINS)
        result = subprocess.run(['sh', str(ROOT / 'docker/caddy/access-mode.sh'), 'env'],
                                env={**values, 'LG_HTTPS_PUBLISHED': 'false',
                                     'LG_S3_AUTHORITY': 'untrusted.test'},
                                capture_output=True, text=True, check=True)
        derived = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        for key, origin in ORIGINS.items():
            self.assertEqual(derived[key.replace('_URL', '_AUTHORITY')], origin.split('://')[1])
        for invalid in ('ftp://gateway.test', 'https://user:password@gateway.test',
                        'https://gateway.test/', 'https://gateway.test/path',
                        'https://gateway.test?query', 'https://gateway.test#fragment',
                        'https://gateway.test:0', 'https://gateway.test:65536',
                        'https://gateway.test:08443', 'https://bad..test',
                        'https://gateway.test\n', 'https://gateway.test"',
                        'https://gateway.test{env.SECRET}'):
            with self.subTest(invalid=invalid), self.assertRaises(bootstrap.Refused):
                bootstrap.access_settings({**base, 'LG_S3_URL': invalid})
        for conflict in (
            {'LG_S3_URL': ORIGINS['LG_LANGFUSE_URL']},
            {'LG_S3_URL': 'https://litellm.localhost:8445'},
            {'LG_S3_URL': 'https://rustfs.localhost:8445'},
            {'LG_S3_URL': 'https://localhost'},
            {'LG_LITELLM_URL': 'https://same.test:443', 'LG_S3_URL': 'https://SAME.test'},
            {'LG_LITELLM_URL': 'http://same.test:80', 'LG_S3_URL': 'https://same.test'},
        ):
            with self.subTest(conflict=conflict), self.assertRaises(bootstrap.Refused):
                bootstrap.access_settings({**base, **ORIGINS, **conflict})
        with self.assertRaises(bootstrap.Refused):
            bootstrap.access_settings({'LG_ACCESS_MODE': 'public', 'LG_PUBLIC_DOMAIN': 'gateway.test',
                                       'LG_S3_URL': 'http://objects.test'})

    def test_langfuse_canonical_origin_is_saved_and_used_before_compose(self):
        for raw, canonical in (("https://LANGFUSE.Example.test:443", "https://langfuse.example.test"),
                               ("http://LANGFUSE.Example.test:80", "http://langfuse.example.test"),
                               ("https://LANGFUSE.Example.test:8444", "https://langfuse.example.test:8444")):
            for from_shell in (False, True):
                with self.subTest(raw=raw, from_shell=from_shell), tempfile.TemporaryDirectory() as tmp:
                    env_file = Path(tmp) / ".env"
                    original = "# operator note\nMY_CUSTOM=kept\nLG_LANGFUSE_URL=" + raw + "\n"
                    if not from_shell:
                        env_file.write_text(original)
                    shell = {"LG_LANGFUSE_URL": raw} if from_shell else {}
                    with patch.dict(os.environ, shell, clear=True):
                        args = ["--env-file", str(env_file), "--render-only"]
                        bootstrap.bootstrap(args)
                        saved = env_file.read_text()
                        self.assertIn("LG_LANGFUSE_URL=" + canonical + "\n", saved)
                        if not from_shell:
                            self.assertTrue(saved.startswith(original))
                        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                        bootstrap.bootstrap(args)
                        self.assertEqual(env_file.read_text(), saved)
                        values = bootstrap.access_settings({"LG_LANGFUSE_URL": raw})
                        self.assertEqual(values["LG_LANGFUSE_URL"], canonical)
                        calls = []
                        def runner(argv):
                            calls.append(argv)
                            return subprocess.CompletedProcess(argv, 0, "", "")
                        bootstrap.compose_up(ROOT, env_file, runner, values["LG_LANGFUSE_URL"])
                        self.assertEqual(calls[0][:3], ["env", "LG_LANGFUSE_URL=" + canonical, "docker"])
                        self.assertEqual(dict(os.environ), shell)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for the static console check')
    def test_console_uses_configured_urls(self):
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
(async () => {
  for (const explicit of [false, true]) {
    const origins = {scheme: 'http', domain: 'localhost', port: ':8080'};
    if (explicit) Object.assign(origins, JSON.parse(process.env.TEST_ORIGINS));
    const links = ['litellm', 'langfuse', 's3', 'rustfs'].map(link => ({dataset: {link, path: '/ui/'}}));
    const element = () => ({children: [], classList: {add() {}},
      setAttribute(name, value) { this[name] = value; },
      addEventListener(name, handler) { this[name] = handler; },
      append(...children) { this.children.push(...children); },
      querySelector(selector) { return this.children.find(c => selector === '.copy' && c.className === 'copy'); }});
    const codes = ['litellm', 'langfuse', 's3', 'rustfs'].map(url => ({
      dataset: {url}, parentElement: element(), closest: () => ({querySelector: () => ({textContent: url})})
    }));
    let copied, clearFeedback, poll;

    const context = {AbortSignal, navigator: {clipboard: {writeText: async value => {copied = value;}}},
      setTimeout: fn => {clearFeedback = fn; return 1;}, clearTimeout() {}, location: {protocol: 'https:', host: 'unrelated.test'},
      fetch: async path => ({ok: true, json: async () => path === '/origins.json' ? origins : {}}),
      document: {createElement: element, querySelectorAll: s => s === '[data-link]' ? links : s === '[data-url]' ? codes : [],
                 querySelector: () => ({})}, setInterval: fn => {poll = fn;}};
    vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
    await new Promise(setImmediate);
    for (const el of links) assert.equal(el.href,
      (explicit ? origins[el.dataset.link] : `http://${el.dataset.link}.localhost:8080`) + '/ui/');
    for (const el of codes) assert.equal(el.textContent,
      explicit ? origins[el.dataset.url] : `http://${el.dataset.url}.localhost:8080`);
    origins.rustfs = 'https://new.test:8449';
    await poll();
    assert.equal(links.find(l => l.dataset.link === 'rustfs').href, 'https://new.test:8449/ui/');
    const endpoint = codes.find(c => c.dataset.url === 'rustfs');
    const copy = endpoint.parentElement.querySelector('.copy');
    const feedback = endpoint.parentElement.children.find(c => c.className === 'copy-status');
    assert.equal(copy.disabled, false);
    await copy.click();
    assert.equal(copied, 'https://new.test:8449', 'copy must use refreshed endpoint');
    assert.equal(feedback.textContent, 'Copied'); clearFeedback(); assert.equal(feedback.textContent, '');
    context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
    await copy.click(); assert.equal(feedback.textContent, 'Select the address to copy');

  }
})();
'''
        subprocess.run(['node', '-e', script, str(ROOT / 'docker/caddy/console/console.js')],
                       env={**os.environ, 'TEST_ORIGINS': json.dumps({
                           key.removeprefix('LG_').removesuffix('_URL').lower(): value
                           for key, value in ORIGINS.items()})}, check=True, capture_output=True, text=True)
