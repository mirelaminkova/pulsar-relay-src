"""Exercise the trust boundary and fail-closed administrator revocation."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from relay_src.policy import admin_members, effective_permissions, trusted_admin
from sync_admins import group_members, write_policy


class SRAMPolicyTest(unittest.TestCase):
    def test_only_authenticated_proxy_can_identify_a_co_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            write_policy(directory, ['alice'])
            path = Path(directory) / 'admins.json'
            key = 'test-proxy-key'
            headers = {b'x-src-user': b'alice', b'x-src-proxy-secret': key.encode()}
            self.assertEqual(trusted_admin(headers, key, path), 'alice')
            self.assertIsNone(trusted_admin({b'x-src-user': b'alice'}, key, path))
            self.assertIsNone(trusted_admin(headers, 'wrong-key', path))
            headers[b'x-src-user'] = b'bob'
            self.assertIsNone(trusted_admin(headers, key, path))
            headers[b'x-src-user'] = b'alice\xff'
            self.assertIsNone(trusted_admin(headers, key, path))

    def test_removal_stale_or_missing_policy_revokes_administration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admins.json'
            self.assertEqual(admin_members(path), set())
            write_policy(directory, ['alice'])
            self.assertEqual(admin_members(path), {'alice'})
            write_policy(directory, [])
            self.assertEqual(admin_members(path), set())
            path.write_text(json.dumps({'members': ['alice'], 'expires_at': time.time() - 1}))
            self.assertEqual(admin_members(path), set())
            path.write_text('broken')
            self.assertEqual(admin_members(path), set())

    def test_bearer_grants_alone_never_create_an_administrator(self):
        self.assertEqual(effective_permissions(['admin', 'read', 'write']), ['read', 'write'])
        self.assertEqual(effective_permissions(['read'], trusted_sram=True), ['admin', 'read', 'write'])

    def test_only_src_managed_groups_are_used_and_lookup_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory) / 'managedgroups'
            managed.write_text('src_co_admin\n')
            with patch('sync_admins.subprocess.run') as run:
                run.return_value.stdout = 'src_co_admin:x:1234:alice,bob\n'
                self.assertEqual(group_members('src_co_admin', managed), ['alice', 'bob'])
                self.assertEqual(group_members('sudo', managed), [])
                run.side_effect = OSError('lookup unavailable')
                self.assertEqual(group_members('src_co_admin', managed), [])


if __name__ == '__main__':
    unittest.main()
