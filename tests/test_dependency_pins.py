"""Check that the relay image installs an exactly pinned dependency set."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def pins():
    result = {}
    for line in (ROOT / 'constraints.txt').read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            match = re.fullmatch(r'([a-z0-9][a-z0-9-]*)==([A-Za-z0-9.+!-]+)', line)
            if match is None:
                raise AssertionError(f'Not an exact, normalised pin: {line!r}')
            result[match.group(1)] = match.group(2)
    return result


class DependencyPinTest(unittest.TestCase):
    def test_every_constraint_is_an_exact_pin_and_core_packages_are_pinned(self):
        pinned = pins()
        # The wrapper patches internals of these packages (see scripts/upstream_contract.py).
        for name in ['fastapi', 'starlette', 'uvicorn', 'pydantic', 'pydantic-settings', 'slowapi',
                     'valkey-glide', 'pwdlib', 'pyjwt']:
            self.assertIn(name, pinned)
        self.assertNotIn('pulsar-relay', pinned, 'upstream itself is pinned by RELAY_REF, not constraints')
        self.assertEqual(len(pinned), len(set(pinned)))

    def test_constraints_match_the_pinned_upstream_commit(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        ref = re.search(r'^ARG RELAY_REF=([0-9a-f]{40})$', dockerfile, re.M).group(1)
        header = re.search(r'^# relay-ref: ([0-9a-f]{40})$', (ROOT / 'constraints.txt').read_text(), re.M)
        self.assertIsNotNone(header)
        self.assertEqual(header.group(1), ref, 'Regenerate constraints.txt when RELAY_REF changes')
        self.assertRegex(dockerfile, r'pip install --no-cache-dir -c /tmp/constraints\.txt')
        self.assertIn('pip check', dockerfile)

    def test_constraints_are_deployed_and_used_by_the_live_test(self):
        playbook = (ROOT / 'playbook.yml').read_text()
        self.assertRegex(playbook, r'\n\s+- constraints\.txt\n')
        live_test = (ROOT / 'scripts' / 'test_relay.py').read_text()
        self.assertIn('"constraints.txt"', live_test)
        self.assertIn('check_image_dependencies(command, root)', live_test)
        self.assertNotIn('constraints.txt', (ROOT / '.dockerignore').read_text())


if __name__ == '__main__':
    unittest.main()
