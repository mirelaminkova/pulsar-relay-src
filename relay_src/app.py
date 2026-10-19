"""SRAM-authenticated management, with upstream bearer auth for machine clients."""
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, Request
from starlette.responses import JSONResponse

from pulsar_relay import main as upstream
from pulsar_relay.auth import dependencies
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.models import User

from .policy import effective_permissions, trusted_admin


config = json.loads(Path(os.environ['PULSAR_CONFIG_FILE']).read_text())
proxy_secret = config['src_proxy_secret']
policy_path = os.environ.get('SRC_ADMIN_POLICY_FILE', '/etc/relay-policy/admins.json')
original_validate = upstream.validate_startup_secrets


def validate_sram_startup(settings):
    if settings.bootstrap_admin_username or settings.bootstrap_admin_password:
        raise RuntimeError('Local bootstrap administrators are disabled; use SRAM CO administration')
    if not isinstance(proxy_secret, str) or len(proxy_secret) != 64:
        raise RuntimeError('Missing SRC proxy authentication secret')
    # Upstream requires a bootstrap password even when no bootstrap user is
    # configured. Satisfy only that check on a copy; keep every other startup
    # guard intact and leave the actual application's bootstrap settings unset.
    original_validate(settings.model_copy(update={'bootstrap_admin_password': 'SRAM-managed'}))


upstream.validate_startup_secrets = validate_sram_startup


async def current_user(request: Request, token_payload=Depends(dependencies.get_token_payload)):
    user = await dependencies.get_current_user(token_payload)
    return user.model_copy(update={'permissions': effective_permissions(
        user.permissions, request.scope.get('src_sram_admin', False))})


upstream.app.dependency_overrides[dependencies.get_current_user] = current_user


class SRAMManagement:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        proxied = b'x-src-proxy-secret' in headers or b'x-src-user' in headers
        if proxied:
            username = trusted_admin(headers, proxy_secret, policy_path)
            if username is None:
                return await JSONResponse({'detail': 'SRAM CO administrator access required'}, 403)(scope, receive, send)
            # Browser cookies authenticate this path, so require a same-origin
            # Origin header for all mutations to prevent cross-site requests.
            if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}:
                origin = headers.get(b'origin', b'').decode('latin1')
                if origin not in upstream.settings.allowed_origins:
                    return await JSONResponse({'detail': 'Same-origin request required'}, 403)(scope, receive, send)
            storage = dependencies.get_user_storage()
            name = 'sram:' + username
            user = await storage.get_user_by_username(name)
            if user is None:
                user = User(user_id=str(uuid4()), username=name, hashed_password=None,
                            permissions=['read', 'write'])
                try:
                    user = await storage.put_user(user)
                except ValueError:
                    user = await storage.get_user_by_username(name)
                    if user is None:
                        raise
            if not user.is_active:
                return await JSONResponse({'detail': 'User is inactive'}, 403)(scope, receive, send)
            # The short-lived token stays inside the server. Neither cookies
            # nor URLs nor response bodies carry a relay administrator token.
            token = create_access_token(user)
            scope = dict(scope, src_sram_admin=True, root_path='/admin')
            scope['headers'] = [(k, v) for k, v in scope['headers']
                                if k not in {b'authorization', b'x-src-user', b'x-src-proxy-secret'}]
            scope['headers'].append((b'authorization', ('Bearer ' + token).encode('ascii')))
        elif scope['path'] in {'/docs', '/redoc', '/openapi.json'}:
            return await JSONResponse({'detail': 'Open /admin/docs through SRAM'}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


app = SRAMManagement(upstream.app)
