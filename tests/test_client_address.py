"""Check X-Forwarded-For trust and the post-deploy client address check."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_client_address as cca


class TrustedProxyConfigurationTest(unittest.TestCase):
    def test_only_docker_bridge_pools_are_trusted_for_forwarded_headers(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertRegex(dockerfile, r'(?m)^ENV FORWARDED_ALLOW_IPS=172\.16\.0\.0/12,192\.168\.0\.0/16$')
        cmd = re.search(r'(?m)^CMD (.*)$', dockerfile).group(1)
        self.assertIn('"--proxy-headers"', cmd)
        # An explicit flag would override the environment; '*' trusts every hop.
        self.assertNotIn('--forwarded-allow-ips', cmd)
        self.assertNotIn('"*"', cmd)

    def test_playbook_checks_client_address_last(self):
        playbook = (ROOT / 'playbook.yml').read_text()
        self.assertIn('        - scripts/check_client_address.py\n', playbook)
        check = playbook.index('scripts/check_client_address.py"')
        self.assertGreater(check, playbook.index('include_tasks: tasks/pulsar.yml'))
        self.assertIn('relay_check_client_address | bool', playbook)
        self.assertIn('relay_check_client_address: true', (ROOT / 'group_vars' / 'all.yml').read_text())


class Recorder(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Recorder.seen.append((self.path, self.headers.get('Host'), self.headers.get('X-Forwarded-For')))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class ClientAddressCheckTest(unittest.TestCase):
    LOG = ('relay-1  | INFO:     {client}:0 - "GET /health?src-client-address-probe={token} HTTP/1.1" 200 OK\n'
           'relay-1  | INFO:     172.18.0.1:4242 - "GET /ready HTTP/1.1" 200 OK\n')

    def test_classifies_forged_gateway_and_real_addresses(self):
        self.assertIsNone(cca.problem('127.0.0.1', '127.0.0.1'))
        self.assertIn('client-supplied', cca.problem(cca.FORGED, '127.0.0.1'))
        for gateway in ['172.17.0.1', '172.31.255.1', '192.168.16.1']:
            self.assertIn('share one rate-limit bucket', cca.problem(gateway, '127.0.0.1'))
        self.assertIn('did not appear', cca.problem(None, '127.0.0.1'))
        self.assertIn('expected 127.0.0.1', cca.problem('10.0.0.5', '127.0.0.1'))

    def test_reads_the_probe_line_from_compose_logs(self):
        logs = self.LOG.format(client='127.0.0.1', token='abc123')
        self.assertEqual(cca.logged_client(logs, 'abc123'), '127.0.0.1')
        self.assertIsNone(cca.logged_client(logs, 'other'))
        self.assertEqual(cca.logged_client(self.LOG.format(client='::1', token='t'), 't'), '::1')

    def test_probe_sends_forged_header_and_public_host(self):
        server = HTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            public = f'http://relay.example.org:{port}'
            issue = cca.check(public, lambda: self.LOG.format(client='127.0.0.1', token=Recorder.seen[-1][0][-16:]),
                              attempts=1)
            self.assertIsNone(issue)
            path, host, forwarded = Recorder.seen[-1]
            self.assertRegex(path, r'^/health\?src-client-address-probe=[0-9a-f]{16}$')
            self.assertEqual(host, f'relay.example.org:{port}')
            self.assertEqual(forwarded, cca.FORGED)
            # The relay logging the forged address is a failure.
            issue = cca.check(public, lambda: self.LOG.format(client=cca.FORGED, token=Recorder.seen[-1][0][-16:]),
                              attempts=1)
            self.assertIn('client-supplied', issue)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
