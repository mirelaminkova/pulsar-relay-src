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
        def __init__(self, username, password, permissions):
            self.username, self.password, self.permissions = username, password, permissions

    models.UserCreate = UserCreate
    package = types.ModuleType('pulsar_relay')
    auth = types.ModuleType('pulsar_relay.auth')
    for name, module in {'pulsar_relay': package, 'pulsar_relay.auth': auth,
                         'pulsar_relay.auth.jwt': jwt, 'pulsar_relay.auth.models': models}.items():
        sys.modules.setdefault(name, module)
    return jwt


class FakeUser:
    def __init__(self, username, hashed_password, permissions):
        self.username, self.hashed_password, self.permissions = username, hashed_password, permissions


class FakeStorage:
    def __init__(self, jwt):
        self.users, self.jwt, self.created, self.updated = {}, jwt, 0, 0

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def create_user(self, data):
        if data.username in self.users:
            raise ValueError('exists')
        self.created += 1
        user = FakeUser(data.username, self.jwt.hash_password(data.password), list(data.permissions))
        self.users[data.username] = user
        return user

    async def update_user(self, user):
        self.updated += 1
        self.users[user.username] = user
        return user


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


if __name__ == '__main__':
    unittest.main()
