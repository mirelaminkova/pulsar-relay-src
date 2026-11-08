"""Container healthcheck: the relay is ready and Valkey still accepts writes.

Upstream /ready only pings storage, and its Valkey check returns a dict that is
always truthy, so it stays green while Valkey refuses every write under
``noeviction`` (publishes and logins then fail with HTTP 500). This probe also
writes a short-lived key and fails when Valkey memory is nearly exhausted.
It needs only the standard library.

Docker does not restart unhealthy containers: a failure shows in
``docker ps`` / ``docker inspect`` and fails the playbook's ``wait`` gate.
"""
import json
import os
import socket
import sys
import urllib.request

# Fail before Valkey is full: at maxmemory every write (and login) fails.
MAX_MEMORY_RATIO = 0.9
PROBE_KEY = 'src:healthcheck'


def _command(*args):
    parts = [f'*{len(args)}\r\n'.encode()]
    for arg in args:
        data = str(arg).encode()
        parts.append(b'$%d\r\n%s\r\n' % (len(data), data))
    return b''.join(parts)


def _reply(stream):
    line = stream.readline()
    if not line.endswith(b'\r\n'):
        raise ConnectionError('Valkey closed the connection')
    kind, value = line[:1], line[1:-2]
    if kind == b'-':
        raise RuntimeError(value.decode(errors='replace'))
    if kind == b'$':
        length = int(value)
        return None if length < 0 else stream.read(length + 2)[:-2]
    if kind in {b'+', b':'}:
        return value
    raise RuntimeError(f'Unexpected Valkey reply type {kind!r}')


def valkey_problem(host, port, password, timeout=3):
    """Return None when Valkey accepts a write and has memory headroom."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        stream = connection.makefile('rb')
        commands = ([('AUTH', password)] if password else []) + [
            ('SET', PROBE_KEY, '1', 'EX', '60'), ('INFO', 'memory')]
        connection.sendall(b''.join(_command(*command) for command in commands))
        replies = []
        for command in commands:
            try:
                replies.append(_reply(stream))
            except RuntimeError as error:
                # Never echo the AUTH arguments; the error text is Valkey's.
                return f'Valkey refused {command[0]}: {error}'
    info = dict(line.split(':', 1) for line in replies[-1].decode().splitlines() if ':' in line)
    used, limit = int(info.get('used_memory', 0)), int(info.get('maxmemory', 0))
    if limit and used / limit > MAX_MEMORY_RATIO:
        return f'Valkey memory at {used / limit:.0%} of maxmemory'
    return None


def ready_problem(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = json.load(response)
    if body.get('ready') is not True:
        return f'relay not ready: {body.get("checks")}'
    return None


def main():
    problem = ready_problem(os.environ.get('SRC_HEALTHCHECK_URL', 'http://127.0.0.1:8080/ready'))
    if problem is None and os.environ.get('PULSAR_STORAGE_BACKEND') == 'valkey':
        config = json.loads(open(os.environ['PULSAR_CONFIG_FILE']).read())
        problem = valkey_problem(os.environ.get('PULSAR_VALKEY_HOST', 'localhost'),
                                 int(os.environ.get('PULSAR_VALKEY_PORT', '6379')),
                                 config.get('valkey_password'))
    if problem:
        print('UNHEALTHY: ' + problem)
        return 1
    print('healthy')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:  # Any failure to probe is unhealthy.
        print(f'UNHEALTHY: {type(error).__name__}: {error}')
        sys.exit(1)
