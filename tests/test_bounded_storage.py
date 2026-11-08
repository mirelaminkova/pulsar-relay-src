"""Check the Valkey bounds in compose.yml and the relay's write/memory healthcheck."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import socketserver
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from relay_src import healthcheck

# Valkey bytes per stream entry, measured with Pulsar 0.15.15 messages (README "Storage limits").
LARGEST_JOB_SETUP = 49202
LARGE_FINAL_STATUS = 81971
SMALL_MESSAGE = 125


def compose_environment():
    text = (ROOT / 'compose.yml').read_text()
    return dict(re.findall(r'^\s+(PULSAR_[A-Z_]+): "?([^"\n]*)"?$', text, re.M)), text


def megabytes(value):
    number, unit = re.fullmatch(r'(\d+)(mb|gb)', value).groups()
    return int(number) * 1024 ** (2 if unit == 'mb' else 3)


class BoundedStorageConfigurationTest(unittest.TestCase):
    def test_messages_and_tokens_are_bounded(self):
        env, _ = compose_environment()
        self.assertEqual(env['PULSAR_PERSISTENT_TIER_RETENTION'], '604800')
        self.assertEqual(env['PULSAR_MAX_MESSAGES_PER_TOPIC'], '2000')
        # Upstream accepts 1-120 minutes and 1-365 days.
        self.assertEqual(env['PULSAR_ACCESS_TOKEN_EXPIRE_MINUTES'], '60')
        self.assertEqual(env['PULSAR_REFRESH_TOKEN_TTL_DAYS'], '7')

    def test_per_topic_cap_fits_in_valkey_memory(self):
        env, _ = compose_environment()
        conf = (ROOT / 'files' / 'valkey.conf').read_text()
        self.assertIn('maxmemory-policy noeviction', conf)
        maxmemory = megabytes(re.search(r'^maxmemory (\S+)$', conf, re.M).group(1))
        cap = int(env['PULSAR_MAX_MESSAGES_PER_TOPIC'])
        # One pair fills job_setup, job_status_update, job_status_request and
        # job_kill to the cap with the largest measured entries.
        worst = cap * (LARGEST_JOB_SETUP + LARGE_FINAL_STATUS + 2 * SMALL_MESSAGE)
        self.assertLess(worst, 0.6 * maxmemory)

    def test_relay_healthcheck_probes_writes(self):
        _, text = compose_environment()
        self.assertIn('test: [CMD, python, -m, relay_src.healthcheck]', text)
        self.assertIn('        - relay_src/healthcheck.py\n', (ROOT / 'playbook.yml').read_text())


class FakeValkey(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), FakeValkeyHandler)
        self.password, self.used, self.maxmemory, self.commands = 'secret', 10, 100, []


class FakeValkeyHandler(socketserver.StreamRequestHandler):
    def read_command(self):
        header = self.rfile.readline()
        if not header:
            return None
        args = []
        for _ in range(int(header[1:])):
            length = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(length + 2)[:-2].decode())
        return args

    def handle(self):
        try:
            self.serve_commands()
        except ConnectionError:
            pass  # The probe stops reading after the first error reply.

    def serve_commands(self):
        server = self.server
        authenticated = server.password is None
        while (args := self.read_command()) is not None:
            server.commands.append(args[0])
            if args[0] == 'AUTH':
                authenticated = args[1] == server.password
                self.wfile.write(b'+OK\r\n' if authenticated else b'-WRONGPASS invalid password\r\n')
            elif not authenticated:
                self.wfile.write(b'-NOAUTH Authentication required.\r\n')
            elif args[0] == 'SET':
                full = server.maxmemory and server.used >= server.maxmemory
                self.wfile.write(b"-OOM command not allowed when used memory > 'maxmemory'.\r\n" if full
                                 else b'+OK\r\n')
            elif args[0] == 'INFO':
                body = f'# Memory\r\nused_memory:{server.used}\r\nmaxmemory:{server.maxmemory}\r\n'.encode()
                self.wfile.write(b'$%d\r\n%s\r\n' % (len(body), body))


@contextmanager
def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def ready_server(body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    return HTTPServer(('127.0.0.1', 0), Handler)


class HealthcheckTest(unittest.TestCase):
    def probe(self, server, password='secret'):
        return healthcheck.valkey_problem('127.0.0.1', server.server_address[1], password, timeout=2)

    def test_healthy_valkey_accepts_a_write(self):
        with serve(FakeValkey()) as server:
            self.assertIsNone(self.probe(server))
            self.assertEqual(server.commands, ['AUTH', 'SET', 'INFO'])
            server.maxmemory = 0  # no limit configured
            server.used = 10 ** 12
            self.assertIsNone(self.probe(server))

    def test_full_or_nearly_full_valkey_is_unhealthy(self):
        with serve(FakeValkey()) as server:
            server.used = 91
            self.assertEqual(self.probe(server), 'Valkey memory at 91% of maxmemory')
            server.used = 100
            problem = self.probe(server)
            self.assertTrue(problem.startswith('Valkey refused SET: OOM'), problem)

    def test_wrong_password_is_unhealthy_without_echoing_it(self):
        with serve(FakeValkey()) as server:
            problem = self.probe(server, password='not-the-password')
            self.assertTrue(problem.startswith('Valkey refused AUTH'), problem)
            self.assertNotIn('not-the-password', problem)

    def test_unreachable_valkey_raises(self):
        server = FakeValkey()
        port = server.server_address[1]
        server.server_close()
        with self.assertRaises(OSError):
            healthcheck.valkey_problem('127.0.0.1', port, 'secret', timeout=1)

    def test_ready_must_be_true(self):
        for body, expected in [({'ready': True, 'checks': {'storage': 'ok'}}, None),
                               ({'ready': False, 'checks': {'storage': 'not_initialized'}},
                                "relay not ready: {'storage': 'not_initialized'}")]:
            with self.subTest(body=body), serve(ready_server(body)) as server:
                url = f'http://127.0.0.1:{server.server_address[1]}/ready'
                self.assertEqual(healthcheck.ready_problem(url), expected)


if __name__ == '__main__':
    unittest.main()
