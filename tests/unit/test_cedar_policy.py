"""Unit tests for the Cedar-subset policy evaluator."""

import asyncio

import pytest

from src.gateway.auth.cedar_policy import (
    CedarPolicyService,
    http_method_to_action,
    parse_policy,
)
from src.gateway.models import AuthMethod, RequestContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(roles=None, project="proj-alpha"):
    return RequestContext(
        user_id="u1",
        project_id=project,
        roles=roles or [],
        scopes=[],
        auth_method=AuthMethod.API_KEY,
    )


PERMIT_READ = {"name": "read", "policy_text": 'permit(principal, action == Action::"read", resource);', "mode": "ENFORCE"}
PERMIT_WRITE = {"name": "write", "policy_text": 'permit(principal, action == Action::"write", resource);', "mode": "ENFORCE"}
FORBID_UNLESS_SENIOR = {"name": "senior", "policy_text": 'forbid(principal, action, resource) unless { principal.role == "senior" };', "mode": "ENFORCE"}
PERMIT_WHEN_ALPHA = {"name": "alpha", "policy_text": 'permit(principal, action == Action::"write", resource) when { principal.project == "proj-alpha" };', "mode": "ENFORCE"}


class TestMethodMapping:
    def test_read_methods(self):
        assert http_method_to_action("GET") == "read"
        assert http_method_to_action("head") == "read"

    def test_write_methods(self):
        assert http_method_to_action("POST") == "write"
        assert http_method_to_action("delete") == "write"


class TestParsing:
    def test_permit_parsed(self):
        assert parse_policy(PERMIT_READ["policy_text"]).effect == "permit"

    def test_forbid_parsed(self):
        assert parse_policy(FORBID_UNLESS_SENIOR["policy_text"]).effect == "forbid"

    def test_unsupported_returns_none(self):
        assert parse_policy("this is not cedar") is None
        assert parse_policy("") is None


class TestDefaultDeny:
    def test_no_matching_permit_denies(self):
        svc = CedarPolicyService([PERMIT_READ])
        # write with only a read-permit -> deny
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "DENY"

    def test_empty_policy_set_denies(self):
        svc = CedarPolicyService([])
        assert _run(svc.evaluate(_ctx(), "get", "/x")) == "DENY"


class TestPermit:
    def test_read_allowed(self):
        svc = CedarPolicyService([PERMIT_READ])
        assert _run(svc.evaluate(_ctx(), "get", "/admin/usage")) == "ALLOW"

    def test_write_allowed_with_write_permit(self):
        svc = CedarPolicyService([PERMIT_READ, PERMIT_WRITE])
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "ALLOW"


class TestForbidOverrides:
    def test_forbid_beats_permit(self):
        # permit write, but forbid-unless-senior applies to a non-senior
        svc = CedarPolicyService([PERMIT_WRITE, FORBID_UNLESS_SENIOR])
        assert _run(svc.evaluate(_ctx(roles=["junior"]), "post", "/api/chat")) == "DENY"

    def test_forbid_exception_lets_senior_through(self):
        svc = CedarPolicyService([PERMIT_WRITE, FORBID_UNLESS_SENIOR])
        assert _run(svc.evaluate(_ctx(roles=["senior"]), "post", "/api/chat")) == "ALLOW"


class TestWhenCondition:
    def test_when_matches_project(self):
        svc = CedarPolicyService([PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-alpha"), "post", "/api/chat")) == "ALLOW"

    def test_when_fails_other_project(self):
        svc = CedarPolicyService([PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"


class TestLogOnlyMode:
    def test_log_only_forbid_does_not_deny(self):
        # A forbid in LOG_ONLY must not affect the decision.
        log_forbid = {**FORBID_UNLESS_SENIOR, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([PERMIT_WRITE, log_forbid])
        assert _run(svc.evaluate(_ctx(roles=["junior"]), "post", "/api/chat")) == "ALLOW"

    def test_log_only_permit_does_not_grant(self):
        log_permit = {**PERMIT_WRITE, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([log_permit])
        # only permit is log-only -> no effective permit -> deny
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "DENY"


class TestRobustness:
    def test_unparseable_policy_skipped_not_crash(self):
        svc = CedarPolicyService([{"name": "bad", "policy_text": "garbage", "mode": "ENFORCE"}, PERMIT_READ])
        assert _run(svc.evaluate(_ctx(), "get", "/x")) == "ALLOW"
