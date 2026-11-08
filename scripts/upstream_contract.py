#!/usr/bin/env python3
"""Check the upstream internals relay_src relies on, inside the relay environment.

scripts/test_relay.py pipes this file into the built relay container
(``docker compose exec -T relay python -``); it can also run in any venv that
has the pinned relay installed, with PULSAR_CONFIG_FILE pointing to a relay
configuration. A failure means a dependency or RELAY_REF change broke an
assumption of the wrapper, so rebuilding the image would not be safe.
"""
from importlib import metadata
import inspect
import re
import sys


def version(name):
    return tuple(int(part) for part in re.findall(r'\d+', metadata.version(name))[:3])


def check():
    from pulsar_relay import config as upstream_config
    from pulsar_relay import main as upstream
    from pulsar_relay.auth import dependencies
    from pulsar_relay.auth.jwt import create_access_token
    import relay_src.app as wrapper

    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    lifespan = inspect.getsource(upstream.lifespan)
    # app.py replaces the module global; lifespan must still look it up by name.
    expect('validate_startup_secrets(settings)' in lifespan,
           'upstream lifespan no longer calls validate_startup_secrets(settings)')
    expect(upstream.validate_startup_secrets is wrapper.validate_sram_startup,
           'validate_startup_secrets monkeypatch is not installed')
    expect('bootstrap_admin_password' in inspect.getsource(upstream_config.validate_startup_secrets),
           'upstream startup check changed its bootstrap password rule')
    expect(upstream.app.router.lifespan_context is wrapper.lifespan,
           'router.lifespan_context wrap is not installed')
    expect(upstream.app.dependency_overrides.get(dependencies.get_current_user) is wrapper.current_user,
           'dependency override for get_current_user is not installed')
    expect(list(inspect.signature(create_access_token).parameters)[:1] == ['user'],
           'create_access_token(user) signature changed')
    expect(callable(getattr(dependencies, 'get_user_storage', None)), 'dependencies.get_user_storage is missing')
    # uvicorn < 0.30 took the leftmost X-Forwarded-For entry even for explicit
    # --forwarded-allow-ips; upstream requires >= 0.32.
    expect(version('uvicorn') >= (0, 30), 'uvicorn is older than 0.30')
    return failures


if __name__ == '__main__':
    problems = check()
    for problem in problems:
        print('FAIL: ' + problem)
    if problems:
        sys.exit(1)
    print('PASS: upstream contract (%s)' % ', '.join(
        f'{name} {metadata.version(name)}' for name in ('pulsar-relay', 'fastapi', 'starlette', 'uvicorn')))
