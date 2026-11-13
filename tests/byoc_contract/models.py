"""Core data types shared by sources, validation, planning and publishing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MASK = "********"


def section_prefs(raw: Any, section: str) -> dict[str, Any] | None:
    """The ``<section>|<input>`` entries of a user's extra preferences.

    Galaxy keeps every extra preference section in one JSON object, including other services' passwords and
    tokens. Only the section byoc-sync works on is kept so nothing else travels through the program.
    Returns ``None`` when ``raw`` is not a JSON object.
    """
    if raw is None or not isinstance(raw, dict):
        return None
    prefix = f"{section}|"
    return {key: value for key, value in raw.items() if isinstance(key, str) and key.startswith(prefix)}


@dataclass(frozen=True)
class UserRecord:
    """One Galaxy user as read from a source.

    ``prefs`` holds only the BYOC section of the user's extra preferences (see :func:`section_prefs`), or
    ``None`` when the user has none or they could not be parsed. ``password_unreadable`` is set when Galaxy
    returned the section without the password value (Galaxy 26.2+ redacts password inputs).
    """

    galaxy_id: str
    email: str
    username: str | None
    deleted: bool
    purged: bool
    active: bool
    prefs: dict[str, Any] | None
    source: str
    password_unreadable: bool = False
    update_time: str | None = None

    def __repr__(self) -> str:
        prefs = None
        if self.prefs is not None:
            prefs = {k: (MASK if "password" in k and v else v) for k, v in self.prefs.items()}
        return (
            f"UserRecord(galaxy_id={self.galaxy_id!r}, email={self.email!r}, username={self.username!r}, "
            f"deleted={self.deleted}, purged={self.purged}, active={self.active}, prefs={prefs!r}, "
            f"source={self.source!r}, password_unreadable={self.password_unreadable})"
        )

    __str__ = __repr__


RABBITMQ = "rabbitmq"
RELAY = "relay"
BACKENDS = (RABBITMQ, RELAY)


@dataclass(frozen=True)
class ByocRequest:
    """A validated BYOC Pulsar request.

    ``backend`` is how Galaxy talks to the endpoint: ``rabbitmq`` (the site's broker, the default) or ``relay``
    (a Pulsar Relay over HTTPS). The ``relay_*`` values are only set for ``relay``; the planner fills in the
    site's defaults for those the user left empty.
    """

    name: str
    password: str = field(repr=False)
    max_cores: int
    max_mem: int | float
    min_gpus: int
    max_gpus: int
    nfs_path: str | None
    backend: str = RABBITMQ
    relay_url: str | None = None
    relay_username: str | None = None
    relay_topic_prefix: str | None = None

    def __repr__(self) -> str:
        relay = ""
        if self.backend != RABBITMQ:
            relay = (
                f", backend={self.backend!r}, relay_url={self.relay_url!r}, relay_username={self.relay_username!r}, "
                f"relay_topic_prefix={self.relay_topic_prefix!r}"
            )
        return (
            f"ByocRequest(name={self.name!r}, password={MASK!r}, max_cores={self.max_cores}, "
            f"max_mem={self.max_mem}, min_gpus={self.min_gpus}, max_gpus={self.max_gpus}, "
            f"nfs_path={self.nfs_path!r}{relay})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class ValidationFailure:
    """Field name -> human readable problem. Never contains submitted secret values."""

    errors: dict[str, str]

    def __str__(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in sorted(self.errors.items()))
