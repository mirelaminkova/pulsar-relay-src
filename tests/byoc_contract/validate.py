"""Strict validation of user-entered BYOC form values.

Everything that reaches a playbook file passes through here first, so the rules are deliberately narrow:
user text ends up inside YAML, Jinja templates and an AMQP URL.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .models import BACKENDS, RABBITMQ, RELAY, ByocRequest, UserRecord, ValidationFailure

FIELDS = (
    "username",
    "password",
    "max_accepted_cores",
    "max_accepted_mem",
    "min_accepted_gpus",
    "max_accepted_gpus",
    "nfs_path",
    "backend",
    "relay_url",
    "relay_username",
    "relay_topic_prefix",
)
REQUIRED = ("username", "password", "max_accepted_cores", "max_accepted_mem")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{10,128}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
# A relay password never goes into a URL, only into the vault and a quoted job_conf value, so it may use the
# characters pulsar-relay accounts commonly have (SRC's pulsar-relay component allows the same set).
RELAY_PASSWORD_RE = re.compile(r"^[A-Za-z0-9._~+=@%-]{10,128}$")
# https only, a DNS host name or IPv4 address, an optional port and path; no credentials, query or fragment.
RELAY_URL_RE = re.compile(
    r"^https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)"
    r"(?::(?P<port>[0-9]{1,5}))?(?P<path>/[A-Za-z0-9._~/-]*)?$"
)
RELAY_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,49}$")
RELAY_TOPIC_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Plain ASCII decimals only: float() would also accept "1e1", "1_0", "+8" or full-width digits.
NUMBER_RE = re.compile(r"[0-9]+(\.[0-9]+)?")

LIMITS = {
    "max_accepted_cores": (1, 1024),
    "min_accepted_gpus": (0, 64),
    "max_accepted_gpus": (0, 64),
}
MIN_MEM_GB = 0.1
MAX_MEM_GB = 16384


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not NUMBER_RE.fullmatch(value):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _int_field(name: str, value: Any, errors: dict[str, str]) -> int | None:
    low, high = LIMITS[name]
    number = _number(value)
    if number is None or not number.is_integer():
        errors[name] = "must be a whole number"
        return None
    if not low <= number <= high:
        errors[name] = f"must be between {low} and {high}"
        return None
    return int(number)


def _mem_field(value: Any, errors: dict[str, str]) -> int | float | None:
    number = _number(value)
    if number is None:
        errors["max_accepted_mem"] = "must be a number (GB)"
        return None
    if not MIN_MEM_GB <= number <= MAX_MEM_GB:
        errors["max_accepted_mem"] = f"must be between {MIN_MEM_GB} and {MAX_MEM_GB} (GB)"
        return None
    return int(number) if number.is_integer() else number


def _name(value: Any, errors: dict[str, str]) -> str | None:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        errors["username"] = "must be 3-32 characters: lowercase letters, digits and '_', starting with a letter"
        return None
    if value.endswith("_") or "__" in value or value.endswith("_byoc"):
        errors["username"] = "must not end with '_' or '_byoc' and must not contain '__'"
        return None
    return value


def is_valid_name(value: str) -> bool:
    """True for a BYOC name byoc-sync could have generated objects for."""
    errors: dict[str, str] = {}
    return _name(value, errors) is not None


def _password(value: Any, errors: dict[str, str], backend: str = RABBITMQ) -> str | None:
    if backend == RELAY:
        if not isinstance(value, str) or not RELAY_PASSWORD_RE.fullmatch(value):
            errors["password"] = "must be 10-128 characters: letters, digits and . _ ~ + = @ % -"
            return None
        return value
    if not isinstance(value, str) or not PASSWORD_RE.fullmatch(value):
        errors["password"] = "must be 10-128 characters, letters and digits only"
        return None
    return value


@dataclass(frozen=True)
class Backends:
    """Which backends a site accepts, which one an empty ``backend`` field means, and the site's own relay (used
    when a relay request leaves ``relay_url`` empty)."""

    allowed: tuple[str, ...] = (RABBITMQ,)
    default: str = RABBITMQ
    relay_url: str | None = None


def _backend(value: Any, backends: Backends, errors: dict[str, str]) -> str | None:
    if value is None:
        return backends.default
    choice = value.lower() if isinstance(value, str) else None
    if choice not in BACKENDS:
        errors["backend"] = f"must be one of {', '.join(BACKENDS)} (empty: {backends.default})"
        return None
    if choice not in backends.allowed:
        errors["backend"] = f"this site only accepts {', '.join(backends.allowed)}"
        return None
    return choice


def relay_url_problem(value: Any) -> str | None:
    """Why ``value`` cannot be a relay URL, or None. Also used for the site config's ``site.relay_url``."""
    match = RELAY_URL_RE.fullmatch(value) if isinstance(value, str) and len(value) <= 255 else None
    if not match:
        return "must be an https:// URL: host name, optional port and path, no credentials, query or fragment"
    port = match.group("port")
    path = match.group("path") or ""
    if port is not None and not 1 <= int(port) <= 65535:
        return "has an invalid port"
    if "//" in path or any(part in (".", "..") for part in path.split("/")[1:]):
        return "must not contain '//' or '.'/'..' segments in its path"
    return None


def _relay_url(value: Any, errors: dict[str, str]) -> str | None:
    problem = relay_url_problem(value)
    if problem:
        errors["relay_url"] = problem
        return None
    match = RELAY_URL_RE.fullmatch(value)
    assert match is not None
    host, port, path = match.group("host").lower(), match.group("port"), (match.group("path") or "").rstrip("/")
    return f"https://{host}{':' + port if port else ''}{path}"


def _pattern(field_name: str, value: Any, regex: re.Pattern[str], rule: str, errors: dict[str, str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not regex.fullmatch(value):
        errors[field_name] = rule
        return None
    return value


def _nfs_path(value: Any, errors: dict[str, str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 255 or not PATH_RE.fullmatch(value):
        errors["nfs_path"] = "must be an absolute path of letters, digits, '.', '_', '-' and '/' (max 255)"
        return None
    stripped = value.rstrip("/")
    if not stripped or "//" in value or any(part in (".", "..") for part in stripped.split("/")[1:]):
        errors["nfs_path"] = "must not be '/', contain '//' or '.'/'..' segments"
        return None
    return stripped


def validate(
    record: UserRecord, section: str = "byoc_pulsar", backends: Backends | None = None
) -> ByocRequest | ValidationFailure | None:
    """Return the validated request, a failure, or ``None`` when the user has not filled in the section.

    The ``relay_*`` fields are only read for the ``relay`` backend; a ``rabbitmq`` request ignores them.
    """
    backends = backends or Backends()
    prefs = record.prefs or {}
    values = {f: _clean(prefs.get(f"{section}|{f}")) for f in FIELDS}
    if all(v is None for v in values.values()):
        return None

    errors: dict[str, str] = {}
    for f in REQUIRED:
        if values[f] is None:
            errors[f] = "is required"

    backend = _backend(values["backend"], backends, errors)
    name = _name(values["username"], errors) if values["username"] is not None else None
    password = _password(values["password"], errors, backend or RELAY) if values["password"] is not None else None
    cores = (
        _int_field("max_accepted_cores", values["max_accepted_cores"], errors)
        if values["max_accepted_cores"] is not None
        else None
    )
    mem = _mem_field(values["max_accepted_mem"], errors) if values["max_accepted_mem"] is not None else None
    min_gpus = _int_field("min_accepted_gpus", values["min_accepted_gpus"] or 0, errors)
    max_gpus = _int_field("max_accepted_gpus", values["max_accepted_gpus"] or 0, errors)
    if min_gpus is not None and max_gpus is not None and min_gpus > max_gpus:
        errors["min_accepted_gpus"] = "must not be greater than max_accepted_gpus"
    nfs_path = _nfs_path(values["nfs_path"], errors)
    relay: dict[str, str | None] = {}
    if backend == RELAY:
        if values["relay_url"] is not None:
            relay["relay_url"] = _relay_url(values["relay_url"], errors)
        elif backends.relay_url:
            relay["relay_url"] = backends.relay_url
        else:
            errors["relay_url"] = "is required for the relay backend"
        relay["relay_username"] = _pattern(
            "relay_username",
            values["relay_username"],
            RELAY_USERNAME_RE,
            "must be 3-50 characters: letters, digits, '.', '_' and '-', starting with a letter or digit",
            errors,
        )
        relay["relay_topic_prefix"] = _pattern(
            "relay_topic_prefix",
            values["relay_topic_prefix"],
            RELAY_TOPIC_PREFIX_RE,
            "must be 1-64 characters: letters, digits, '.', '_' and '-', starting with a letter or digit",
            errors,
        )

    if errors:
        return ValidationFailure(errors)
    assert name and password and cores is not None and mem is not None
    return ByocRequest(
        name=name,
        password=password,
        max_cores=cores,
        max_mem=mem,
        min_gpus=min_gpus or 0,
        max_gpus=max_gpus or 0,
        nfs_path=nfs_path,
        **({"backend": RELAY, **relay} if backend == RELAY else {}),
    )
