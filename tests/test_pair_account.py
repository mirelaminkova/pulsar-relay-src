"""Check the built-in pair account bootstrap without the relay package installed."""
import asyncio
import hashlib
import importlib
from pathlib import Path
import sys
import types
import unittest


def install_stubs():
    """Provide the two upstream modules relay_src.pair imports."""
    jwt = types.ModuleType('pulsar_relay.auth.jwt')
    jwt.hash_password = lambda password: 'hash:' + hashlib.sha256(password.encode()).hexdigest()
    jwt.verify_password = lambda password, hashed: hashed == jwt.hash_password(password)
    models = types.ModuleType('pulsar_relay.auth.models')

    class UserCreate:
        def __init__(self, username, password, permissions, email=None):
            self.username, self.password, self.permissions, self.email = username, password, permissions, email

    models.UserCreate = UserCreate
    package = types.ModuleType('pulsar_relay')
    auth = types.ModuleType('pulsar_relay.auth')
    for name, module in {'pulsar_relay': package, 'pulsar_relay.auth': auth,
                         'pulsar_relay.auth.jwt': jwt, 'pulsar_relay.auth.models': models}.items():
        sys.modules.setdefault(name, module)
    return jwt


class FakeUser:
    def __init__(self, username, hashed_password, permissions, email=None, is_active=True):
        self.username, self.hashed_password, self.permissions = username, hashed_password, permissions
        self.user_id, self.email, self.is_active = 'id-' + username, email, is_active


class FakeStorage:
    def __init__(self, jwt):
        self.users, self.jwt, self.created, self.updated = {}, jwt, 0, 0

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def create_user(self, data):
        if data.username in self.users:
            raise ValueError('exists')
        self.created += 1
        user = FakeUser(data.username, self.jwt.hash_password(data.password), list(data.permissions), data.email)
        self.users[data.username] = user
        return user

    async def update_user(self, user):
        self.updated += 1
        self.users[user.username] = user
        return user

    async def list_users(self):
        return list(self.users.values())


class FakeToken:
    def __init__(self, jti, user_id):
        self.jti, self.user_id, self.revoked, self.revoked_reason = jti, user_id, False, None


class FakeRefreshStorage:
    """Mirrors upstream RefreshTokenStorage.list_for_user / mark_revoked."""

    def __init__(self):
        self.tokens = {}

    def issue(self, user_id):
        token = FakeToken(f'jti-{len(self.tokens)}', user_id)
        self.tokens[token.jti] = token
        return token

    async def list_for_user(self, user_id, *, include_revoked=False):
        return [t for t in self.tokens.values() if t.user_id == user_id and (include_revoked or not t.revoked)]

    async def mark_revoked(self, jti, reason):
        token = self.tokens[jti]
        if not token.revoked:
            token.revoked, token.revoked_reason = True, reason


class PairAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jwt = install_stubs()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        cls.pair = importlib.import_module('relay_src.pair')

    def test_creates_non_admin_account_once_and_rotates_password(self):
        storage = FakeStorage(self.jwt)
        run = asyncio.run
        user = run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64))
        self.assertEqual(user.permissions, ['read', 'write'])
        self.assertEqual((storage.created, storage.updated), (1, 0))
        run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64))
        self.assertEqual((storage.created, storage.updated), (1, 0))
        run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'q' * 64))
        self.assertEqual((storage.created, storage.updated), (1, 1))
        self.assertTrue(self.jwt.verify_password('q' * 64, storage.users['galaxy-pulsar'].hashed_password))

    def test_strips_stored_admin_grant_and_tolerates_missing_configuration(self):
        storage = FakeStorage(self.jwt)
        storage.users['galaxy-pulsar'] = FakeUser('galaxy-pulsar', self.jwt.hash_password('p' * 64),
                                                 ['admin', 'read', 'write'])
        user = asyncio.run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64))
        self.assertEqual(user.permissions, ['read', 'write'])
        self.assertEqual(storage.updated, 1)
        self.assertIsNone(asyncio.run(self.pair.ensure_pair_account(storage, None, None)))
        self.assertEqual((storage.created, storage.updated), (0, 1))

    def test_password_change_revokes_refresh_tokens_but_a_restart_does_not(self):
        storage, refresh = FakeStorage(self.jwt), FakeRefreshStorage()
        run = asyncio.run
        user = run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64, refresh))
        galaxy, pulsar = refresh.issue(user.user_id), refresh.issue(user.user_id)
        other = refresh.issue('id-someone-else')
        # Restart with the same password: nothing is revoked or rewritten.
        run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64, refresh))
        self.assertFalse(galaxy.revoked or pulsar.revoked)
        self.assertEqual(storage.updated, 0)
        with self.assertLogs('relay_src.pair', 'WARNING') as logs:
            run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'q' * 64, refresh))
        self.assertTrue(galaxy.revoked and pulsar.revoked)
        self.assertEqual((galaxy.revoked_reason, pulsar.revoked_reason), ('admin', 'admin'))
        self.assertFalse(other.revoked)
        self.assertIn('revoked 2 refresh tokens', logs.output[0])
        self.assertNotIn('q' * 64, ' '.join(logs.output))

    def test_existing_untagged_pair_account_is_adopted_without_changing_its_password(self):
        storage = FakeStorage(self.jwt)
        hashed = self.jwt.hash_password('p' * 64)
        storage.users['galaxy-pulsar'] = FakeUser('galaxy-pulsar', hashed, ['read', 'write'])
        user = asyncio.run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64, FakeRefreshStorage()))
        self.assertEqual(user.email, self.pair.PAIR_MARKER)
        self.assertEqual(user.hashed_password, hashed)
        self.assertTrue(user.is_active)

    def test_rename_deactivates_only_the_tagged_previous_pair_account(self):
        storage, refresh = FakeStorage(self.jwt), FakeRefreshStorage()
        run = asyncio.run
        old = run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64, refresh))
        old_token = refresh.issue(old.user_id)
        # Accounts the module did not create stay untouched.
        storage.users['second-pair'] = FakeUser('second-pair', self.jwt.hash_password('s' * 64), ['read', 'write'],
                                                email='ops@example.org')
        storage.users['sram:alice'] = FakeUser('sram:alice', None, ['read', 'write'])
        with self.assertLogs('relay_src.pair', 'WARNING') as logs:
            new = run(self.pair.ensure_pair_account(storage, 'renamed-pair', 'r' * 64, refresh))
        self.assertTrue(new.is_active)
        self.assertFalse(storage.users['galaxy-pulsar'].is_active)
        self.assertTrue(old_token.revoked)
        self.assertTrue(storage.users['second-pair'].is_active)
        self.assertTrue(storage.users['sram:alice'].is_active)
        self.assertIn('Deactivated previous built-in pair account galaxy-pulsar', logs.output[0])
        # Renaming back adopts the old account but does not silently reactivate it.
        with self.assertLogs('relay_src.pair', 'WARNING') as logs:
            run(self.pair.ensure_pair_account(storage, 'galaxy-pulsar', 'p' * 64, refresh))
        self.assertFalse(storage.users['galaxy-pulsar'].is_active)
        self.assertFalse(storage.users['renamed-pair'].is_active)
        self.assertTrue(any('is inactive' in line for line in logs.output))


if __name__ == '__main__':
    unittest.main()
