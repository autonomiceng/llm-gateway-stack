"""Exercise the real bounded HTTP child against an owned loopback server."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from status_io import Unavailable, run
from status_probes import http


class HttpTests(unittest.TestCase):
    def test_http_status_and_size_bounds(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'x' * 262145 if self.path == '/large' else b'ready'
                self.send_response(404 if self.path == '/missing' else 200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(http('127.0.0.1', port, '/ready', run), 'ready')
            self.assertIsNone(http('127.0.0.1', port, '/missing', run))
            with self.assertRaises(Unavailable):
                http('127.0.0.1', port, '/large', run)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
