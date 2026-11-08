"""Create the built-in Galaxy/Pulsar pair account from the deployment configuration."""
import logging

from pulsar_relay.auth.jwt import hash_password, verify_password
from pulsar_relay.auth.models import UserCreate

log = logging.getLogger(__name__)
PAIR_PERMISSIONS = ['read', 'write']
# Marks accounts this module manages, so a rename deactivates only those and
# never an account an administrator registered. Upstream users have no other
# free-form field; ".invalid" can never be a deliverable address.
PAIR_MARKER = 'src-pair-account@pulsar-relay.invalid'


async def revoke_refresh_tokens(refresh_storage, user):
    """Revoke every refresh token of ``user``; return how many were revoked."""
    if refresh_storage is None:
        return 0
    tokens = await refresh_storage.list_for_user(user.user_id)
    for token in tokens:
        await refresh_storage.mark_revoked(token.jti, 'admin')
    return len(tokens)


async def deactivate_previous_pair_accounts(storage, refresh_storage, username):
    """Deactivate pair accounts this module created under an earlier username."""
    for user in await storage.list_users():
        if user.username == username or user.email != PAIR_MARKER or not user.is_active:
            continue
        user.is_active = False
        await storage.update_user(user)
        revoked = await revoke_refresh_tokens(refresh_storage, user)
        log.warning('Deactivated previous built-in pair account %s (pair account is now %s) and revoked %d '
                    'refresh tokens. Galaxy and Pulsar must both use %s.', user.username, username, revoked,
                    username)


async def ensure_pair_account(storage, username, password, refresh_storage=None):
    """Create the non-admin pair account, or update its password after rotation.

    Mirrors upstream's bootstrap-admin startup step, but the account never
    carries the ``admin`` permission: administration stays with SRAM. When the
    password changes, sessions started with the old one are revoked; accounts
    this module created under another username are deactivated.
    """
    if not username or not password:
        log.warning('No built-in pair account configured; register pair accounts through /admin/docs')
        return None
    user = await storage.get_user_by_username(username)
    if user is None:
        try:
            user = await storage.create_user(UserCreate(username=username, email=PAIR_MARKER, password=password,
                                                        permissions=PAIR_PERMISSIONS))
        except ValueError:
            # Another worker or an earlier start created it concurrently.
            user = await storage.get_user_by_username(username)
            if user is None:
                raise
        else:
            log.info('Created built-in pair account %s', username)
            await deactivate_previous_pair_accounts(storage, refresh_storage, username)
            return user
    changed = rotated = False
    if 'admin' in user.permissions:
        user.permissions = [p for p in user.permissions if p != 'admin']
        changed = True
    if user.email != PAIR_MARKER:
        # Accounts created before the marker existed, or adopted by configuration.
        user.email = PAIR_MARKER
        changed = True
    if not user.is_active:
        # Left as an administrator (or an earlier rename) set it.
        log.warning('Built-in pair account %s is inactive; reactivate it through /admin/docs', username)
    if user.hashed_password is None or not verify_password(password, user.hashed_password):
        user.hashed_password = hash_password(password)
        changed = rotated = True
    if changed:
        user = await storage.update_user(user)
        log.info('Updated built-in pair account %s from the deployment configuration', username)
    if rotated:
        revoked = await revoke_refresh_tokens(refresh_storage, user)
        log.warning('Pair account %s password changed: revoked %d refresh tokens issued with the old password',
                    username, revoked)
    await deactivate_previous_pair_accounts(storage, refresh_storage, username)
    return user
