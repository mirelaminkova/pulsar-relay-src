#!/usr/bin/env python3
"""Create persistent secrets and Compose settings without third-party packages."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit


def write_private(path, content):
    if path.exists() and path.read_text() == content:
        os.chmod(path, 0o600)
        return False
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(content)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return True


SECRET_NAMES = ("PULSAR_JWT_SECRET_KEY", "PULSAR_VALKEY_PASSWORD", "PULSAR_SRC_PROXY_SECRET",
                "PULSAR_PAIR_PASSWORD")
# Matches the relay's UserCreate limits (3-50 characters) without shell or YAML metacharacters.
PAIR_USERNAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{2,49}"
# A pair password chosen in the SRC wizard. No quotes, spaces, commas, colons or
# brackets, so it survives the one-line list parameters of the SRC Galaxy component.
PAIR_PASSWORD_PATTERN = r"[A-Za-z0-9._~+=@%-]{16,128}"


def configure(directory, public_url, port=8080, pair_username="galaxy-pulsar", pair_password=None):
    parsed = urlsplit(public_url)
    local = parsed.hostname in {"localhost", "127.0.0.1"}
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
        raise ValueError("Use an HTTPS public URL, or HTTP localhost for local tests")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("The public URL must be an origin without credentials or a path")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
        raise ValueError("Use a DNS hostname or IPv4 address")
    # Accessing .port also validates malformed/out-of-range public URL ports.
    if parsed.port == 0:
        raise ValueError("Public URL port cannot be zero")
    if not 1024 <= port <= 65535:
        raise ValueError("Relay port must be between 1024 and 65535")
    if not re.fullmatch(PAIR_USERNAME_PATTERN, pair_username or ""):
        raise ValueError("The pair username must be 3-50 characters of letters, digits, '.', '_' or '-'")
    if pair_password and not re.fullmatch(PAIR_PASSWORD_PATTERN, pair_password):
        raise ValueError("The pair password must be 16-128 characters of letters, digits or ._~+=@%-")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    secret_path = directory / "secrets.json"
    if secret_path.exists():
        values = json.loads(secret_path.read_text())
    else:
        values = {name: secrets.token_hex(32) for name in SECRET_NAMES}
    # Preserve JWT/storage state when upgrading the previous bootstrap-admin
    # layout, but remove that local administrator credential entirely.
    legacy_names = {'PULSAR_JWT_SECRET_KEY', 'PULSAR_VALKEY_PASSWORD', 'PULSAR_BOOTSTRAP_ADMIN_PASSWORD'}
    if isinstance(values, dict) and set(values) == legacy_names:
        for name in legacy_names:
            if not isinstance(values[name], str) or not re.fullmatch(r'[a-f0-9]{64}', values[name]):
                raise ValueError('Invalid legacy deployment credential')
        values.pop('PULSAR_BOOTSTRAP_ADMIN_PASSWORD')
        values['PULSAR_SRC_PROXY_SECRET'] = secrets.token_hex(32)
    # Deployments made before the built-in Galaxy/Pulsar pair account keep
    # every existing secret and gain only the generated pair password.
    if isinstance(values, dict) and set(values) == set(SECRET_NAMES) - {'PULSAR_PAIR_PASSWORD'}:
        values['PULSAR_PAIR_PASSWORD'] = secrets.token_hex(32)
    if not isinstance(values, dict) or set(values) != set(SECRET_NAMES):
        raise ValueError("secrets.json must contain exactly the four deployment secrets")
    # A password supplied through the SRC wizard replaces the stored one. When
    # the parameter is left empty, the stored (generated) password is kept.
    if pair_password:
        values['PULSAR_PAIR_PASSWORD'] = pair_password
    for name in SECRET_NAMES:
        pattern = PAIR_PASSWORD_PATTERN if name == 'PULSAR_PAIR_PASSWORD' else r"[a-f0-9]{64}"
        if not isinstance(values.get(name), str) or not re.fullmatch(pattern, values[name]):
            raise ValueError(f"{name} has an invalid format")
    changed = write_private(secret_path, json.dumps(values, indent=2) + "\n")
    private = directory / '.secrets'
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    relay_config = {key.removeprefix('PULSAR_').lower(): value for key, value in values.items()}
    # The relay creates this non-admin machine account at startup so a Galaxy
    # and Pulsar pair can connect without a manual registration step.
    relay_config['pair_username'] = pair_username
    # Each service gets only its own file. The host directory is owner-only;
    # files are readable inside the container by its unprivileged service user.
    for name, content in {
        'relay.json': json.dumps(relay_config) + '\n',
        'valkey.conf': (directory / 'files' / 'valkey.conf').read_text() +
                       '\nrequirepass ' + values['PULSAR_VALKEY_PASSWORD'] + '\n',
    }.items():
        path = private / name
        changed = write_private(path, content) or changed
        os.chmod(path, 0o444)
    settings = dict(RELAY_PORT=str(port))
    text = "# Generated by scripts/configure.py; keep private.\n"
    text += "".join(f"{key}={value}\n" for key, value in settings.items())
    text += "PULSAR_TRUSTED_HOSTS='" + json.dumps([parsed.hostname, "localhost", "127.0.0.1"]) + "'\n"
    text += "PULSAR_ALLOWED_ORIGINS='" + json.dumps([public_url.rstrip("/")]) + "'\n"
    return write_private(directory / ".env", text) or changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--pair-username", default="galaxy-pulsar",
                        help="Built-in non-admin relay account shared by one Galaxy/Pulsar pair")
    args = parser.parse_args()
    # Read from the environment rather than argv so the password never shows in the process list.
    password = os.environ.get("RELAY_PAIR_PASSWORD", "").strip() or None
    print("changed" if configure(args.directory, args.public_url, args.port, args.pair_username, password)
          else "unchanged")
