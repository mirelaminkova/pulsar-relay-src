#!/usr/bin/env python3
"""Check which client address the relay sees for a request through the proxy.

Sends one request with a forged X-Forwarded-For through nginx (connecting to
it on loopback) and reads the relay's access log. The relay must log the
address nginx saw (127.0.0.1), not the forged one, which would let anyone
choose their rate-limit bucket, and not a Docker bridge address, which would
put every client into one shared login rate-limit bucket.
"""
import argparse
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

FORGED = '203.0.113.77'  # TEST-NET-3, never a real client
DOCKER_POOLS = [ipaddress.ip_network(n) for n in ('172.16.0.0/12', '192.168.0.0/16')]


class LoopbackHTTPS(http.client.HTTPSConnection):
    """HTTPS to the public name (SNI, Host) over a connection to another address."""

    def __init__(self, connect_host, server_name, port, context):
        super().__init__(server_name, port, timeout=15, context=context)
        self.connect_host, self.tls = connect_host, context

    def connect(self):
        sock = socket.create_connection((self.connect_host, self.port), self.timeout)
        self.sock = self.tls.wrap_socket(sock, server_hostname=self.host)


def send_probe(public_url, connect_host='127.0.0.1', forwarded_for=FORGED):
    """GET /health through the proxy at connect_host; return the probe token."""
    url = urlsplit(public_url)
    token = secrets.token_hex(8)
    port = url.port or (443 if url.scheme == 'https' else 80)
    if url.scheme == 'https':
        # Our own nginx on loopback, and the probe carries no secret: the
        # certificate is not what this check is about.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = LoopbackHTTPS(connect_host, url.hostname, port, context)
    else:
        connection = http.client.HTTPConnection(connect_host, port, timeout=15)
    host = url.hostname if url.port is None else f'{url.hostname}:{url.port}'
    connection.request('GET', '/health?src-client-address-probe=' + token,
                       headers={'Host': host, 'X-Forwarded-For': forwarded_for})
    response = connection.getresponse()
    response.read()
    connection.close()
    if response.status != 200:
        raise RuntimeError(f'Probe through {connect_host}:{port} returned HTTP {response.status}')
    return token


def logged_client(logs, token):
    """The client address uvicorn logged for the probe, or None."""
    pattern = re.compile(r'INFO:\s+(\S+):\d+ - "GET /health\?src-client-address-probe=' + token + ' ')
    found = [match.group(1) for match in map(pattern.search, logs.splitlines()) if match]
    return found[-1] if found else None


def problem(client, expected):
    if client is None:
        return 'the probe did not appear in the relay access log'
    if client == FORGED:
        return 'the relay trusts a client-supplied X-Forwarded-For (check --forwarded-allow-ips)'
    try:
        if any(ipaddress.ip_address(client) in pool for pool in DOCKER_POOLS):
            return (f'the relay sees the Docker gateway {client} for every client, so all clients share one '
                    'rate-limit bucket (FORWARDED_ALLOW_IPS does not include the gateway, or nginx sets no '
                    'X-Forwarded-For)')
    except ValueError:
        pass
    if client != expected:
        return f'the relay saw {client}, expected {expected}'
    return None


def check(public_url, read_logs, connect_host='127.0.0.1', forwarded_for=FORGED, expected='127.0.0.1',
          attempts=5):
    token = send_probe(public_url, connect_host, forwarded_for)
    for attempt in range(attempts):
        client = logged_client(read_logs(), token)
        if client is not None or attempt == attempts - 1:
            return problem(client, expected)
        time.sleep(1)  # The log driver may lag behind the response.


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--directory', required=True, help='Compose project directory')
    parser.add_argument('--project', default='pulsar-relay')
    args = parser.parse_args()

    def compose_logs():
        return subprocess.run(['docker', 'compose', '--project-directory', args.directory, '-p', args.project,
                               'logs', '--no-color', '--since', '10m', 'relay'],
                              check=True, capture_output=True, text=True).stdout

    issue = check(args.public_url, compose_logs)
    if issue:
        sys.exit('Client address check failed: ' + issue)
    print('Relay sees the real client address through the proxy')
