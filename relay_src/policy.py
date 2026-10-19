"""Fail-closed authorization of identities supplied by the SRC SRAM proxy."""
import hmac
import json
from pathlib import Path
import re
import time


def admin_members(path, now=None):
    now = time.time() if now is None else now
    try:
        policy = json.loads(Path(path).read_text())
        expiry = policy['expires_at']
        members = policy['members']
        if not isinstance(expiry, (int, float)) or not now < expiry <= now + 300:
            return set()
        if not isinstance(members, list) or any(not isinstance(x, str) for x in members):
            return set()
        return {x for x in members if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.@-]{0,127}', x)}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def trusted_admin(headers, expected_secret, policy_path):
    """Never trust a username without the proxy's private authentication key."""
    supplied = headers.get(b'x-src-proxy-secret', b'')
    username = headers.get(b'x-src-user', b'').decode('ascii', errors='ignore')
    if not expected_secret or not hmac.compare_digest(supplied, expected_secret.encode('ascii')):
        return None
    if headers.get(b'x-src-user', b'') != username.encode('ascii'):
        return None
    return username if username in admin_members(policy_path) else None


def effective_permissions(permissions, trusted_sram=False):
    """Stored grants and old bearer tokens cannot independently grant admin."""
    return ['admin', 'read', 'write'] if trusted_sram else [p for p in permissions if p != 'admin']
