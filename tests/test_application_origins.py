"""Application origin contracts without Docker or application databases."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap

ORIGINS = {f'LG_{app}_URL': f'https://darkforge.tail694fe2.ts.net:{port}'
           for app, port in (('LITELLM', 8443), ('LANGFUSE', 8444), ('S3', 8445), ('CONSOLE', 8446), ('GRAFANA', 8447), ('BACKPLANE', 8448))}


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
            if key in ('LG_GRAFANA_URL', 'LG_BACKPLANE_URL'):
                continue
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
    const links = ['litellm', 'langfuse', 's3', 'grafana', 'backplane'].map(link => ({
      dataset: {link, path: '/ui/'}, disabled: true,
      closest: () => ['grafana', 'backplane'].includes(link) ? {dataset: {ready: 'true'}} : null,
      removeAttribute(name) { if (name === 'aria-disabled') this.disabled = false; }
    }));
    const codes = ['litellm', 'langfuse', 's3', 'grafana', 'backplane'].map(url => ({dataset: {url}}));
    const context = {location: {protocol: 'https:', host: 'unrelated.test'},
      fetch: async path => ({ok: true, json: async () => path === '/origins.json' ? origins : {}}),
      document: {querySelectorAll: s => s === '[data-link]' ? links : s === '[data-url]' ? codes : [],
                 querySelector: () => ({})}, setInterval: () => {}};
    vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
    await new Promise(setImmediate);
    for (const el of links) assert.equal(el.href,
      (explicit ? origins[el.dataset.link] : `http://${el.dataset.link}.localhost:8080`) + '/ui/');
    for (const el of links) assert.equal(el.disabled, false, 'ready links must be accessible after origins load');
    for (const el of codes) assert.equal(el.textContent,
      explicit ? origins[el.dataset.url] : `http://${el.dataset.url}.localhost:8080`);
  }
})();
'''
        subprocess.run(['node', '-e', script, str(ROOT / 'docker/caddy/console/console.js')],
                       env={**os.environ, 'TEST_ORIGINS': json.dumps({
                           key.removeprefix('LG_').removesuffix('_URL').lower(): value
                           for key, value in ORIGINS.items()})}, check=True, capture_output=True, text=True)
