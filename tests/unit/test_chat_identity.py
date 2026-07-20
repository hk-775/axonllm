"""Regression tests for task #3 — chat identity must come from the authenticated
context, never from the request body/query params.

Guards against tenant impersonation: a caller must not be able to set user_id or
project_id in the body to attribute quota/budget/model-access to another tenant.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.gateway.chat.client_agent import ClientAgent
from src.gateway.chat.routes import _identity_from_context
from src.gateway.models import AuthMethod, RequestContext


# ---------------------------------------------------------------------------
# _identity_from_context — the trust boundary
# ---------------------------------------------------------------------------


def _request_with_context(ctx):
    return SimpleNamespace(state=SimpleNamespace(context=ctx))


class TestIdentityFromContext:
    def test_api_key_context_used(self):
        ctx = RequestContext(
            user_id="apikey:k1", project_id="project-b", roles=["service"],
            scopes=[], auth_method=AuthMethod.API_KEY, api_key_id="k1",
        )
        assert _identity_from_context(_request_with_context(ctx)) == ("apikey:k1", "project-b")

    def test_oidc_context_used(self):
        ctx = RequestContext(
            user_id="alice", project_id="proj-alpha", roles=["dev"],
            scopes=[], auth_method=AuthMethod.OIDC_JWT,
        )
        assert _identity_from_context(_request_with_context(ctx)) == ("alice", "proj-alpha")

    def test_anonymous_context_returns_none(self):
        # LOG_ONLY / dev — do not trust for attribution; fall back to defaults downstream.
        ctx = RequestContext(
            user_id="anonymous", project_id="", roles=[], scopes=[],
            auth_method=AuthMethod.ANONYMOUS,
        )
        assert _identity_from_context(_request_with_context(ctx)) == (None, None)

    def test_missing_context_returns_none(self):
        assert _identity_from_context(SimpleNamespace(state=SimpleNamespace())) == (None, None)


# ---------------------------------------------------------------------------
# ClientAgent — attribution follows the passed (context) identity, not defaults
# ---------------------------------------------------------------------------


class _CapturingGateway:
    def __init__(self):
        self.captured = []
        self.cost_tracker = SimpleNamespace(_records=[])
        self._user_configs = {}

    async def handle_chat_completion(self, request_data, context):
        self.captured.append(dict(context))
        return {
            "id": "x", "model": "m", "provider": "p",
            "choices": [{"message": {"content": "ok"}}], "usage": {},
        }


class TestClientAgentAttribution:
    def test_authenticated_identity_wins(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw, default_user_id="chat-user", default_project_id="chat-project")
        asyncio.run(ca.chat(
            "m", [{"role": "user", "content": "hi"}],
            user_id="apikey:k1", project_id="project-b",
        ))
        assert gw.captured[-1]["user_id"] == "apikey:k1"
        assert gw.captured[-1]["project_id"] == "project-b"

    def test_anonymous_falls_back_to_defaults(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw, default_user_id="chat-user", default_project_id="chat-project")
        asyncio.run(ca.chat(
            "m", [{"role": "user", "content": "hi"}],
            user_id=None, project_id=None,
        ))
        assert gw.captured[-1]["user_id"] == "chat-user"
        assert gw.captured[-1]["project_id"] == "chat-project"
