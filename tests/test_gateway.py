"""Gateway readiness probes for bootstrap and restore; Docker is never called."""
import ssl
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap  # noqa: E402
import checkpoint  # noqa: E402


class GatewayProbeTests(unittest.TestCase):
    def test_https_probes_use_local_address_and_verified_public_hostname(self):
        settings = {'LG_ACCESS_MODE': 'public', 'LG_SCHEME': 'https', 'LG_PUBLIC_DOMAIN': 'gateway.test',
                    'LG_TLS_ISSUER': 'acme', 'LG_BIND_HOST': '0.0.0.0', 'LG_HTTPS_PORT': '18453'}
        with patch.object(bootstrap, 'wait_ready') as wait:
            bootstrap.probe_gateway(settings, [], Mock())
        args, kwargs = wait.call_args
        self.assertEqual(args[0], 'https://127.0.0.1:18453/health/langfuse')
        self.assertEqual(kwargs['host'], 'gateway.test')
        self.assertTrue(kwargs['context'].check_hostname)
        self.assertEqual(kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        sock = Mock()
        with patch.object(bootstrap.socket, 'create_connection', return_value=sock) as connect, \
             patch.object(ssl.SSLContext, 'wrap_socket', side_effect=ssl.SSLCertVerificationError('bad certificate')) as wrap:
            connection = bootstrap.LocalHTTPSConnection('gateway.test', 18453, '127.0.0.1', kwargs['context'])
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
            connect.assert_called_once_with(('127.0.0.1', 18453), 5)
            wrap.assert_called_once_with(sock, server_hostname='gateway.test')
            sock.close.assert_called_once()

    def test_restore_trusts_own_internal_ca_and_uses_http_behind_edge(self):
        stack = Mock()
        env = {'LG_ACCESS_MODE': 'local', 'LG_SCHEME': 'https', 'LG_LISTEN_SCHEME': 'dual',
               'LG_PUBLIC_DOMAIN': 'gateway.test', 'LG_TLS_ISSUER': 'internal'}
        stack.config = {'services': {'caddy': {'environment': env, 'ports': [
            {'target': 80, 'published': 18090, 'host_ip': '127.0.0.1'},
            {'target': 443, 'published': 18453, 'host_ip': '127.0.0.1'}]}}}
        stack.command = ['docker', 'compose', '--env-file', '/private/drill.env']
        stack.runner.return_value = subprocess.CompletedProcess([], 0, 'internal-root-pem', '')
        with patch.object(ssl.SSLContext, 'load_verify_locations') as load, \
             patch.object(bootstrap, 'wait_ready') as wait:
            checkpoint.health(stack)
            load.assert_called_once_with(cadata='internal-root-pem')
            context = wait.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(wait.call_args.kwargs['host'], 'gateway.test')
            stack.runner.assert_called_once_with(stack.command + [
                'exec', '-T', 'caddy', 'cat', '/data/caddy/pki/authorities/local/root.crt'])
            stack.runner.reset_mock()
            wait.reset_mock()
            env['LG_ACCESS_MODE'] = 'proxy'
            env['LG_TRUSTED_PROXIES'] = '172.30.0.0/24'
            checkpoint.health(stack)
            stack.runner.assert_not_called()
            self.assertEqual(wait.call_args.args[0], 'http://127.0.0.1:18090/health/langfuse')
            self.assertIsNone(wait.call_args.kwargs['context'])
            env['LG_ACCESS_MODE'] = 'local'
            stack.runner.return_value = subprocess.CompletedProcess([], 1, '', 'private error')
            wait.reset_mock()
            with self.assertRaises(bootstrap.Refused) as error:
                checkpoint.health(stack)
            self.assertEqual(error.exception.code, 'internal_ca_unavailable')
            self.assertEqual(wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
