"""Create the built-in Galaxy/Pulsar pair account from the deployment configuration."""
import logging

from pulsar_relay.auth.jwt import hash_password, verify_password
from pulsar_relay.auth.models import UserCreate

log = logging.getLogger(__name__)
PAIR_PERMISSIONS = ['read', 'write']


async def ensure_pair_account(storage, username, password):
    """Create the non-admin pair account, or update its password after rotation.

    Mirrors upstream's bootstrap-admin startup step, but the account never
    carries the ``admin`` permission: administration stays with SRAM.
    """
    if not username or not password:
        log.warning('No built-in pair account configured; register pair accounts through /admin/docs')
        return None
    user = await storage.get_user_by_username(username)
    if user is None:
        try:
            user = await storage.create_user(UserCreate(username=username, password=password,
                                                        permissions=PAIR_PERMISSIONS))
        except ValueError:
            # Another worker or an earlier start created it concurrently.
            user = await storage.get_user_by_username(username)
            if user is None:
                raise
        else:
            log.info('Created built-in pair account %s', username)
            return user
    changed = False
    if 'admin' in user.permissions:
        user.permissions = [p for p in user.permissions if p != 'admin']
        changed = True
    if user.hashed_password is None or not verify_password(password, user.hashed_password):
        user.hashed_password = hash_password(password)
        changed = True
    if changed:
        user = await storage.update_user(user)
        log.info('Updated built-in pair account %s from the deployment configuration', username)
    return user
