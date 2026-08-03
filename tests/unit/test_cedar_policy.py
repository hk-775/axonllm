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


class TestClausesThatWouldSilentlyWidenAStatement:
    """Scope equalities the evaluator cannot honour must not parse.

    The parser only reads the action out of the scope triple. A `principal ==
    User::"alice"` or `resource == Resource::"/api/chat"` clause therefore had no
    effect — and both narrow a statement, so ignoring them *widens* it: a forbid
    meant for one endpoint forbade every write, and a permit meant for one user
    permitted everyone. Rejecting them turns a silent authorization change into a
    400 from `POST /admin/policies`.
    """

    def test_resource_equality_is_rejected(self):
        assert parse_policy(
            'forbid(principal, action == Action::"write", resource == Resource::"/api/chat");'
        ) is None

    def test_principal_equality_is_rejected(self):
        assert parse_policy('permit(principal == User::"alice", action, resource);') is None

    def test_principal_membership_is_rejected(self):
        """`in` is Cedar's group test, equally unimplemented here."""
        assert parse_policy('permit(principal in Group::"eng", action, resource);') is None

    def test_resource_membership_is_rejected(self):
        assert parse_policy('forbid(principal, action, resource in Folder::"admin");') is None

    def test_action_membership_is_rejected(self):
        """An action *set* is not the single-action equality the evaluator reads."""
        assert parse_policy(
            'permit(principal, action in [Action::"read", Action::"write"], resource);'
        ) is None

    def test_an_effect_with_no_scope_triple_is_rejected(self):
        """`permit;` is not Cedar, and it used to parse as "permit everything".

        A typo or a truncated statement became the most permissive policy
        expressible, which is the wrong direction for an authorization failure.
        """
        assert parse_policy("permit;") is None
        assert parse_policy("forbid;") is None
        assert parse_policy("permit") is None

    def test_the_bare_variables_are_still_fine(self):
        """`resource` on its own means "any resource" — which is exactly what the
        evaluator does, so it must keep parsing."""
        assert parse_policy("permit(principal, action, resource);") is not None
        assert parse_policy(
            'forbid(principal, action == Action::"write", resource);'
        ) is not None

    def test_principal_attribute_conditions_are_untouched(self):
        """`principal.role == "senior"` in a when/unless body is supported and
        must not be caught by the scope-clause rejection."""
        stmt = parse_policy(
            'forbid(principal, action == Action::"write", resource) '
            'unless { principal.role == "senior" };'
        )
        assert stmt is not None
        assert stmt.conditions_unless[0].attr == "role"

    def test_a_condition_value_containing_scope_syntax_is_not_rejected(self):
        """The rejection reads the scope triple, not the whole statement.

        A tenant, project, or role name is operator-supplied text that can
        legitimately contain `==` or the word `in`. Matching against the whole
        statement would reject a valid policy because of a value it never
        inspects, and the operator would have no way to tell why.
        """
        stmt = parse_policy(
            'permit(principal, action, resource) '
            'when { principal.project == "action in review" };'
        )
        assert stmt is not None
        assert stmt.conditions_when[0].value == "action in review"

        stmt = parse_policy(
            'forbid(principal, action, resource) '
            'unless { principal.role == "resource == owner" };'
        )
        assert stmt is not None
        assert stmt.conditions_when == []
        assert stmt.conditions_unless[0].value == "resource == owner"

    def test_the_action_comes_from_the_scope_not_the_condition_body(self):
        """An action clause misplaced into a `when` body must not retarget the
        statement — reading it from anywhere in the text would narrow a
        blanket forbid to reads and let every write through.

        The statement below is what "forbid junior reads" looks like with the
        `&&` left out. The condition regex tolerates the trailing text, so the
        policy is accepted with the author's *stated* scope: `action` unqualified,
        which governs everything.
        """
        stmt = parse_policy(
            'forbid(principal, action, resource) '
            'when { principal.role == "junior" action == Action::"read" };'
        )
        assert stmt is not None
        assert stmt.action is None, (
            "the misplaced Action::\"read\" was read as the statement's action, "
            "so this forbid would no longer cover writes"
        )
        ctx = _ctx(roles=["junior"])
        svc = CedarPolicyService([
            {"name": "typo", "mode": "ENFORCE", "policy_text":
             'forbid(principal, action, resource) '
             'when { principal.role == "junior" action == Action::"read" };'}
        ])
        assert _run(svc.evaluate(ctx, "post", "/api/chat")) == "DENY"
        assert _run(svc.evaluate(ctx, "get", "/admin/overview")) == "DENY"

    def test_a_rejected_clause_is_skipped_at_startup_not_fatal(self):
        """Construction still tolerates one — a stored policy from before this
        check must not stop the gateway booting."""
        svc = CedarPolicyService([
            {"name": "old", "mode": "ENFORCE",
             "policy_text": 'forbid(principal, action, resource == Resource::"/x");'},
            PERMIT_WHEN_ALPHA,
        ])
        assert _run(svc.evaluate(_ctx(project="proj-alpha"), "post", "/x")) == "ALLOW"


class TestDefaultDeny:
    """Default deny, within the actions the policy set governs.

    See `TestAnActionWithNoPolicyAboutItIsNotDenied` for why the scope is per
    action rather than global.
    """

    def test_no_matching_permit_denies(self):
        # `write` is governed (PERMIT_WHEN_ALPHA names it) but the condition
        # fails for this caller, so there is no matching permit -> deny.
        svc = CedarPolicyService([PERMIT_READ, PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"

    def test_empty_policy_set_governs_nothing(self):
        """An empty set denies nothing — the same outcome as `bootstrap.py`
        wiring no policy service at all when the list is empty, so enabling
        Cedar with zero policies is not a silent outage."""
        svc = CedarPolicyService([])
        assert _run(svc.evaluate(_ctx(), "get", "/x")) == "ALLOW"
        assert _run(svc.evaluate(_ctx(), "post", "/x")) == "ALLOW"


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
        """A LOG_ONLY permit contributes no permission of its own.

        Visible where an ENFORCE statement governs the action: `write` is
        deny-by-default because of the enforced permit, and the LOG_ONLY permit
        does not rescue a caller the enforced one excludes.
        """
        log_permit = {**PERMIT_WRITE, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([log_permit, PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"


class TestRobustness:
    def test_unparseable_policy_skipped_not_crash(self):
        svc = CedarPolicyService([{"name": "bad", "policy_text": "garbage", "mode": "ENFORCE"}, PERMIT_READ])
        assert _run(svc.evaluate(_ctx(), "get", "/x")) == "ALLOW"


class TestAnActionWithNoPolicyAboutItIsNotDenied:
    """Default-deny is scoped to actions some policy actually governs.

    Textbook Cedar denies anything no `permit` covers. Applied to a gateway whose
    policy set is *authored incrementally over HTTP*, that rule turns the first
    policy you add into an outage: `permit(… Action::"read" …)` says nothing about
    writes, and strict default-deny reads that silence as "forbid every write" —
    including `POST /admin/policies`, so the follow-up policy that would fix it
    cannot be submitted. The gateway is unrecoverable without a restart.

    So deny is scoped per action: an action is governed once any ENFORCE
    statement mentions it (by name, or by omitting the action clause and thus
    covering all of them). Within a governed action, Cedar's rules are intact —
    default deny, and forbid beats permit, both asserted above and below. An
    ungoverned action falls through to the layers that are always on:
    authentication, admin RBAC, and quota enforcement.

    The alternative — denying by default and telling operators to write an
    allow-all policy first — puts the safe configuration one forgotten step away
    from a self-inflicted outage, and the failure is silent until someone tries
    to write.
    """

    def test_a_read_only_policy_does_not_brick_writes(self):
        """The exact policy the README suggests first."""
        svc = CedarPolicyService([PERMIT_READ])
        assert _run(svc.evaluate(_ctx(), "get", "/admin/overview")) == "ALLOW"
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "ALLOW"
        assert _run(svc.evaluate(_ctx(), "post", "/admin/policies")) == "ALLOW"

    def test_a_write_only_policy_does_not_brick_reads(self):
        svc = CedarPolicyService([PERMIT_WRITE])
        assert _run(svc.evaluate(_ctx(), "get", "/admin/overview")) == "ALLOW"

    def test_a_governed_action_still_defaults_to_deny(self):
        """The half of Cedar that must survive: once a policy governs `write`,
        a write that matches no permit is denied."""
        svc = CedarPolicyService([PERMIT_WHEN_ALPHA])  # write, only for proj-alpha
        assert _run(svc.evaluate(_ctx(project="proj-alpha"), "post", "/api/chat")) == "ALLOW"
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"

    def test_an_actionless_statement_governs_every_action(self):
        """`forbid(principal, action, resource)` names no action, so it governs
        all of them — otherwise a blanket forbid would deny nothing."""
        forbid_all = {"name": "lockdown", "mode": "ENFORCE",
                      "policy_text": "forbid(principal, action, resource);"}
        svc = CedarPolicyService([forbid_all])
        for method in ("get", "post", "put", "delete"):
            assert _run(svc.evaluate(_ctx(), method, "/x")) == "DENY", method

    def test_an_actionless_permit_governs_every_action(self):
        """The same rule where only the deny-by-default path can show it.

        A blanket forbid denies through the forbid branch, so it would pass even
        if actionless statements governed nothing. An actionless *permit* whose
        condition fails has no permit to match, so the caller is denied only if
        the statement is understood to govern the action — which is the whole
        point of a policy of the form "only seniors, for anything".
        """
        permit_seniors_anything = {
            "name": "seniors-only", "mode": "ENFORCE",
            "policy_text": 'permit(principal, action, resource) when { principal.role == "senior" };',
        }
        svc = CedarPolicyService([permit_seniors_anything])
        for method in ("get", "post", "put", "delete"):
            assert _run(svc.evaluate(_ctx(roles=["junior"]), method, "/x")) == "DENY", method
            assert _run(svc.evaluate(_ctx(roles=["senior"]), method, "/x")) == "ALLOW", method

    def test_a_forbid_scoped_to_write_leaves_reads_alone(self):
        forbid_write = {"name": "no-writes", "mode": "ENFORCE",
                        "policy_text": 'forbid(principal, action == Action::"write", resource);'}
        svc = CedarPolicyService([forbid_write])
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "DENY"
        assert _run(svc.evaluate(_ctx(), "get", "/admin/overview")) == "ALLOW"

    def test_a_conditional_permit_still_governs_the_action_it_names(self):
        """Governance comes from the action clause, not from whether the
        condition happened to hold for this caller — otherwise a policy would
        silently stop applying to exactly the callers it was written to exclude."""
        svc = CedarPolicyService([PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"

    def test_the_seeded_demo_set_still_behaves(self):
        """The demo seed permits read and write explicitly, so nothing about it
        depends on the scoping change."""
        svc = CedarPolicyService([PERMIT_READ, PERMIT_WRITE])
        assert _run(svc.evaluate(_ctx(), "get", "/x")) == "ALLOW"
        assert _run(svc.evaluate(_ctx(), "post", "/x")) == "ALLOW"


class TestLogOnlyPoliciesGovernNothing:
    """A LOG_ONLY policy must be observably inert.

    This is the sharpest form of the bug: `POST /admin/policies` defaults `mode`
    to `LOG_ONLY`, and LOG_ONLY is *documented* as the safe way to trial a policy
    before enforcing it. Under strict default-deny, adding one LOG_ONLY policy to
    an unpoliced gateway denied every request — the safe path was the one that
    caused the outage.
    """

    def test_a_lone_log_only_policy_denies_nothing(self):
        log_only = {**PERMIT_READ, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([log_only])
        for method in ("get", "post", "put", "patch", "delete", "head"):
            assert _run(svc.evaluate(_ctx(), method, "/api/chat")) == "ALLOW", method

    def test_log_only_does_not_make_an_action_governed(self):
        """A LOG_ONLY write-permit must not switch `write` into default-deny;
        that would let an observation-only policy change the outcome."""
        log_only_write = {**PERMIT_WHEN_ALPHA, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([log_only_write])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "ALLOW"

    def test_an_enforce_policy_alongside_it_still_governs(self):
        log_only = {**PERMIT_READ, "mode": "LOG_ONLY"}
        svc = CedarPolicyService([log_only, PERMIT_WHEN_ALPHA])
        assert _run(svc.evaluate(_ctx(project="proj-beta"), "post", "/api/chat")) == "DENY"
        assert _run(svc.evaluate(_ctx(project="proj-alpha"), "post", "/api/chat")) == "ALLOW"

    def test_an_unparseable_policy_governs_nothing(self):
        """It is skipped at construction, so it must not silently switch an
        action into deny-by-default on the strength of text nobody could parse."""
        svc = CedarPolicyService([{"name": "bad", "policy_text": "garbage", "mode": "ENFORCE"}])
        assert _run(svc.evaluate(_ctx(), "post", "/api/chat")) == "ALLOW"
