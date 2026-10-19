#!/usr/bin/env python3
"""Exercise the real relay and Valkey in a disposable Docker Compose project."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from configure import configure
from sync_admins import write_policy


def request(base, path, method="GET", data=None, token=None, form=False, expected=200, extra_headers=None):
    headers = dict(extra_headers or {})
    body = None
    if data is not None:
        body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    if status != expected:
        # Never include auth responses, credentials or token-bearing request bodies.
        raise RuntimeError(f"{method} {path}: expected HTTP {expected}, received {status}")
    return json.loads(payload) if payload else None


def login(base, username, password):
    return request(base, "/auth/login", "POST", {"username": username, "password": password}, form=True)["access_token"]


def run_test(root, keep=False):
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for the live integration test; install Docker Engine and Compose v2")
    subprocess.run(["docker", "compose", "version"], check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(root)
    work = Path(tempfile.mkdtemp(prefix="relay-test-", dir=root / ".local-test"))
    project = "relay-test-" + secrets.token_hex(4)
    command = ["docker", "compose", "--project-directory", str(work), "-p", project]
    try:
        for relative in ["Dockerfile", ".dockerignore", "compose.yml", "files/valkey.conf"]:
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        shutil.copytree(root / 'relay_src', work / 'relay_src', ignore=shutil.ignore_patterns('__pycache__'))
        base = f"http://127.0.0.1:{port}"
        configure(work, base, port)
        write_policy(work / '.policy', ['integration-admin'])
        subprocess.run(command + ["up", "--build", "--wait", "--wait-timeout", "180"], check=True)
        request(base, "/ready")
        request(base, "/api/v1/topics", expected=401)
        values = json.loads((work / "secrets.json").read_text())
        write_policy(work / '.policy', ['integration-admin'])
        admin_headers = {'X-SRC-User': 'integration-admin',
                         'X-SRC-Proxy-Secret': values['PULSAR_SRC_PROXY_SECRET'], 'Origin': base}
        request(base, '/auth/users', extra_headers=admin_headers)
        schema = request(base, '/openapi.json', extra_headers=admin_headers)
        if not any(server['url'] == '/admin' for server in schema.get('servers', [])):
            raise RuntimeError('Management API schema did not preserve the /admin proxy prefix')
        request(base, '/auth/register', 'POST', {'username': 'blocked', 'password': 'not-used'},
                expected=403, extra_headers={key: value for key, value in admin_headers.items() if key != 'Origin'})
        request(base, '/auth/users', extra_headers={'X-SRC-User': 'integration-admin'}, expected=403)
        request(base, '/auth/users', extra_headers=dict(admin_headers, **{'X-SRC-User': 'non-admin'}), expected=403)
        write_policy(work / '.policy', [])
        request(base, '/auth/users', extra_headers=admin_headers, expected=403)
        write_policy(work / '.policy', ['integration-admin'])
        username, password = "integration-user", secrets.token_hex(24)
        # Even a stale/manually stored admin grant must not give a machine
        # account administrative authority independently of SRAM membership.
        request(base, "/auth/register", "POST", {"username": username, "password": password,
                "permissions": ["admin", "read", "write"]}, expected=201, extra_headers=admin_headers)
        token = login(base, username, password)
        request(base, "/auth/users", token=token, expected=403)
        topic = "src-installation-probe"
        request(base, "/api/v1/topics", "POST", {"topic_name": topic}, token, expected=201)
        payload = {"probe": secrets.token_hex(16), "state": "queued"}
        request(base, "/api/v1/messages", "POST", {"topic": topic, "payload": payload}, token, expected=201)
        poll = {"topics": [topic], "since": {topic: "0-0"}, "timeout": 1}
        result = request(base, "/messages/poll", "POST", poll, token)
        if not any(message["payload"] == payload for message in result["messages"]):
            raise RuntimeError("Published message was not delivered through long polling")
        print("PASS: SRAM admin mapping/revocation, forged-header rejection, machine permissions and message delivery")
        # Recreate both containers to prove storage survives beyond their lifetimes.
        # The test's named volume is kept by down (without --volumes).
        subprocess.run(command + ["down"], check=True)
        subprocess.run(command + ["up", "--wait", "--wait-timeout", "180"], check=True)
        token = login(base, username, password)
        result = request(base, "/messages/poll", "POST", poll, token)
        if not any(message["payload"] == payload for message in result["messages"]):
            raise RuntimeError("Message did not survive container recreation")
        print("PASS: user, topic and message persisted after container recreation")
    finally:
        if keep:
            print(f"Test project retained in {work} (Compose project {project})")
        else:
            # Only this randomly named disposable project is removed.
            subprocess.run(command + ["down", "--volumes", "--remove-orphans"], check=False)
            shutil.rmtree(work)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Keep the isolated test project for inspection")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    (root / ".local-test").mkdir(exist_ok=True)
    try:
        run_test(root, args.keep)
    except (RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        parser.exit(1, f"Integration test failed: {error}\n")
