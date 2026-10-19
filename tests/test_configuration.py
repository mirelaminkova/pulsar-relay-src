"""Check credential retention and input handling without deployment privileges."""
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from configure import configure


def prepare(directory):
    path = Path(directory) / 'files'
    path.mkdir()
    source = Path(__file__).resolve().parents[1] / 'files' / 'valkey.conf'
    (path / 'valkey.conf').write_text(source.read_text())


class ConfigurationTest(unittest.TestCase):
    def test_rerun_preserves_credentials_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            self.assertTrue(configure(directory, "https://relay.example.org"))
            path = Path(directory)
            original = (path / "secrets.json").read_bytes()
            self.assertFalse(configure(directory, "https://relay.example.org"))
            self.assertTrue(configure(directory, "https://new.example.org"))
            self.assertEqual(original, (path / "secrets.json").read_bytes())
            for name in ["secrets.json", ".env"]:
                self.assertEqual(stat.S_IMODE((path / name).stat().st_mode), 0o600)
            values = json.loads(original)
            self.assertEqual(len(set(values.values())), 3)

    def test_rejects_remote_http_and_dotenv_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            for url in ["http://relay.example.org", "https://a.example/api", "https://user:pass@a.example",
                        "https://a.example?x=1", "https://a.example#fragment", "https://bad'name.example",
                        "https://a.example:bogus", "https://a.example:70000", "https://a.example:0"]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    configure(directory, url)
            self.assertFalse((Path(directory) / "secrets.json").exists())

    def test_accepts_local_test_and_rejects_corrupt_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            configure(directory, "http://127.0.0.1:18080", 18080)
            (Path(directory) / "secrets.json").write_text('{"PULSAR_JWT_SECRET_KEY":"weak"}')
            with self.assertRaises(ValueError):
                configure(directory, "http://localhost:18080", 18080)

    def test_secrets_are_not_in_dotenv_or_compose_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            configure(directory, 'https://relay.example.org')
            path = Path(directory)
            values = json.loads((path / 'secrets.json').read_text())
            dotenv = (path / '.env').read_text()
            compose_path = Path(__file__).resolve().parents[1] / 'compose.yml'
            compose = compose_path.read_text()
            for secret in values.values():
                self.assertNotIn(secret, dotenv)
            for key in values:
                self.assertNotIn(key, compose)
            self.assertNotIn('--requirepass', compose)
            self.assertEqual(stat.S_IMODE((path / '.secrets').stat().st_mode), 0o700)
            relay = json.loads((path / '.secrets' / 'relay.json').read_text())
            self.assertEqual(relay['jwt_secret_key'], values['PULSAR_JWT_SECRET_KEY'])
            self.assertIn('requirepass ' + values['PULSAR_VALKEY_PASSWORD'],
                          (path / '.secrets' / 'valkey.conf').read_text())
            self.assertNotIn('bootstrap_admin_password', relay)
            self.assertNotIn('BOOTSTRAP_ADMIN', dotenv)

    def test_upgrade_removes_local_admin_without_changing_storage_or_jwt_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            path = Path(directory)
            old = {'PULSAR_JWT_SECRET_KEY': 'a' * 64, 'PULSAR_VALKEY_PASSWORD': 'b' * 64,
                   'PULSAR_BOOTSTRAP_ADMIN_PASSWORD': 'c' * 64}
            (path / 'secrets.json').write_text(json.dumps(old))
            configure(directory, 'https://relay.example.org')
            new = json.loads((path / 'secrets.json').read_text())
            for name in ['PULSAR_JWT_SECRET_KEY', 'PULSAR_VALKEY_PASSWORD']:
                self.assertEqual(old[name], new[name])
            self.assertNotIn('PULSAR_BOOTSTRAP_ADMIN_PASSWORD', new)
            self.assertIn('PULSAR_SRC_PROXY_SECRET', new)


if __name__ == "__main__":
    unittest.main()
