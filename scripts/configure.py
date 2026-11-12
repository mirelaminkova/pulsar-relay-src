#!/usr/bin/env python3
"""Create persistent secrets and Compose settings without third-party packages."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import string
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

# BYOC mode (Galaxy's byoc_pulsar form). The BYOC name follows byoc-sync's rules for the
# form's username (byoc-sync/src/byoc_sync/validate.py): 3-32 of a-z 0-9 _, starting with a
# letter, not ending in _ or _byoc, without __. The pair account is then named like the
# BYOC objects byoc-sync creates: galaxy_<name>_byoc.
BYOC_NAME_PATTERN = r"[a-z][a-z0-9_]{2,31}"
BYOC_PAIR_USERNAME = "galaxy_{name}_byoc"
# A BYOC pair password is letters and digits only: valid as the relay form's
# relay_password and the MQ form's password, and safe unquoted in YAML and URLs.
BYOC_PASSWORD_PATTERN = r"[A-Za-z0-9]{16,128}"
BYOC_PASSWORD_LENGTH = 32
PAIR_STATE = "pair.json"


def is_valid_byoc_name(name):
    return bool(isinstance(name, str) and re.fullmatch(BYOC_NAME_PATTERN, name)
                and not name.endswith("_") and "__" not in name and not name.endswith("_byoc"))


def is_valid_byoc_password(password):
    """Letters and digits that YAML reads back as the same string.

    Galaxy's job_conf.yml templates print runner parameters unquoted (the BYOC
    relay sync checks this too), so a password YAML reads as an integer is
    refused: only digits, or a 0x (hexadecimal) or 0b (binary) literal.
    """
    return bool(isinstance(password, str) and re.fullmatch(BYOC_PASSWORD_PATTERN, password)
                and not re.fullmatch(r"[0-9]+|0x[0-9A-Fa-f]+|0b[01]+", password))


def byoc_pair_username(name):
    return BYOC_PAIR_USERNAME.format(name=name)


def generate_byoc_name():
    return "src" + secrets.token_hex(4)


def generate_byoc_password():
    alphabet = string.ascii_letters + string.digits
    # A leading letter keeps every generated password away from YAML's numbers.
    return secrets.choice(string.ascii_letters) + "".join(
        secrets.choice(alphabet) for _ in range(BYOC_PASSWORD_LENGTH - 1))


def read_pair_state(directory):
    """The non-secret pair account record (pair.json), or an empty one before the first run."""
    path = Path(directory) / PAIR_STATE
    if not path.exists():
        # Deployments made before pair.json: the account in the relay configuration.
        try:
            relay = json.loads((Path(directory) / ".secrets" / "relay.json").read_text())
        except (OSError, ValueError):
            relay = {}
        previous = relay.get("pair_username") if isinstance(relay, dict) else None
        return {"pair_username": previous, "byoc_name": None, "retired_pair_usernames": []}
    state = json.loads(path.read_text())
    if not (isinstance(state, dict)
            and set(state) == {"byoc", "byoc_name", "pair_username", "retired_pair_usernames"}
            and isinstance(state["retired_pair_usernames"], list)
            and all(isinstance(name, str) and re.fullmatch(PAIR_USERNAME_PATTERN, name)
                    for name in state["retired_pair_usernames"])
            and (state["byoc_name"] is None or is_valid_byoc_name(state["byoc_name"]))
            and (state["pair_username"] is None
                 or re.fullmatch(PAIR_USERNAME_PATTERN, str(state["pair_username"])))):
        raise ValueError(f"{PAIR_STATE} is not the pair account record this component writes")
    return state


def configure(directory, public_url, port=8080, pair_username="galaxy-pulsar", pair_password=None,
              byoc=False, byoc_name=None):
    """Write the deployment secrets and settings; return whether anything changed.

    With ``byoc`` the pair account is named galaxy_<BYOC name>_byoc (``pair_username``
    is then ignored): ``byoc_name`` when given, else the stored one, else a generated
    one. The account in effect, the BYOC name and the earlier pair accounts the relay
    must deactivate are recorded in pair.json.
    """
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
    if byoc_name and not is_valid_byoc_name(byoc_name):
        raise ValueError("The BYOC name must be 3-32 of a-z 0-9 _, start with a letter, "
                         "not end in _ or _byoc, and not contain __")
    if byoc and pair_password and not is_valid_byoc_password(pair_password):
        raise ValueError("In BYOC mode the pair password must be 16-128 letters and digits, "
                         "not only digits")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pair_state = read_pair_state(directory)
    # The BYOC name is kept even while BYOC mode is off, so switching back keeps the account.
    effective_byoc_name = byoc_name or pair_state.get("byoc_name") or (generate_byoc_name() if byoc else None)
    if byoc:
        pair_username = byoc_pair_username(effective_byoc_name)
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
    elif byoc and not is_valid_byoc_password(values.get('PULSAR_PAIR_PASSWORD')):
        # A stored password chosen before BYOC mode may contain characters the BYOC
        # form refuses; the account changes its name anyway, so it gets a new one.
        values['PULSAR_PAIR_PASSWORD'] = generate_byoc_password()
    for name in SECRET_NAMES:
        pattern = PAIR_PASSWORD_PATTERN if name == 'PULSAR_PAIR_PASSWORD' else r"[a-f0-9]{64}"
        if not isinstance(values.get(name), str) or not re.fullmatch(pattern, values[name]):
            raise ValueError(f"{name} has an invalid format")
    changed = write_private(secret_path, json.dumps(values, indent=2) + "\n")
    # An earlier pair account (another name, or the account before BYOC mode) still
    # holds read/write on the relay; the relay deactivates it at startup.
    retired = [name for name in pair_state["retired_pair_usernames"] if name != pair_username]
    previous = pair_state.get("pair_username")
    if previous and previous != pair_username and previous not in retired:
        retired.append(previous)
    pair_state = {"byoc": bool(byoc), "byoc_name": effective_byoc_name,
                  "pair_username": pair_username, "retired_pair_usernames": retired}
    changed = write_private(directory / PAIR_STATE, json.dumps(pair_state, indent=2) + "\n") or changed
    private = directory / '.secrets'
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    relay_config = {key.removeprefix('PULSAR_').lower(): value for key, value in values.items()}
    # The relay creates this non-admin machine account at startup so a Galaxy
    # and Pulsar pair can connect without a manual registration step.
    relay_config['pair_username'] = pair_username
    relay_config['retired_pair_usernames'] = retired
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
    parser.add_argument("--byoc", action="store_true",
                        help="Galaxy BYOC mode: name the pair account galaxy_<BYOC name>_byoc")
    parser.add_argument("--byoc-name", default="",
                        help="BYOC name (byoc-sync rules); empty keeps the stored or generates one")
    args = parser.parse_args()
    # Read from the environment rather than argv so the password never shows in the process list.
    password = os.environ.get("RELAY_PAIR_PASSWORD", "").strip() or None
    print("changed" if configure(args.directory, args.public_url, args.port, args.pair_username, password,
                                 byoc=args.byoc, byoc_name=args.byoc_name.strip() or None)
          else "unchanged")
