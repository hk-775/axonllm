"""Cedar-subset policy evaluation for gateway authorization.

Implements the ``PolicyService`` protocol used by ``AuthMiddleware``. Policies
are Cedar ``permit``/``forbid`` statements stored as text (see demo_seed.yaml).
This evaluator supports the practical subset AxonLLM uses:

    permit(principal, action == Action::"read", resource);
    forbid(principal, action, resource) unless { principal.role == "senior" };
    permit(principal, action, resource) when { principal.project == "proj-alpha" };

Semantics follow Cedar's core rules, with default-deny scoped per action:
  * An action is *governed* once some enforcing statement mentions it (by name,
    or by omitting the action clause and so covering every action).
  * Within a governed action: default deny — allowed only if some ``permit``
    matches — and ``forbid`` overrides ``permit``.
  * An action no policy governs is ALLOW; see ``CedarPolicyService`` for why.
  * A statement matches when its principal/action/resource scope matches AND
    its ``when`` condition holds AND its ``unless`` condition does not.

Scope clauses supported: the bare variable (``principal``, ``resource``) or an
action equality (``action == Action::"read"``). Principal and resource
*equalities* are deliberately rejected rather than ignored — a dropped
``principal == User::"alice"`` widens a statement from one user to everyone, and a
dropped ``resource == Resource::"/api/chat"`` widens it from one endpoint to all
of them. Both fail open, so they must not parse.

Conditions support ``principal.<attr> == "value"`` (and its negation) joined by
``&&``. Anything the parser does not understand returns None, and the caller's
options are to reject it (``POST /admin/policies``, with a 400) or skip it with a
warning (startup, so one bad stored policy cannot stop the gateway booting).

This is a pure-Python evaluator — no native Cedar dependency.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.gateway.models import RequestContext

logger = logging.getLogger(__name__)

# HTTP method -> Cedar action name. Policies speak in coarse actions ("read",
# "write") rather than raw HTTP verbs.
_METHOD_TO_ACTION = {
    "get": "read",
    "head": "read",
    "options": "read",
    "post": "write",
    "put": "write",
    "patch": "write",
    "delete": "write",
}


def http_method_to_action(method: str) -> str:
    """Map an HTTP method to the coarse Cedar action name."""
    return _METHOD_TO_ACTION.get(method.lower(), method.lower())


@dataclass
class _Condition:
    """A single ``principal.attr == value`` clause, optionally negated."""

    attr: str
    value: str
    negated: bool

    def holds(self, ctx: RequestContext) -> bool:
        if self.attr == "role":
            # Cedar treats role as scalar; a caller may hold several roles, so
            # equality means "has this role".
            equal = self.value in ctx.roles
        else:
            equal = _principal_attr(ctx, self.attr) == self.value
        return (not equal) if self.negated else equal


@dataclass
class _Statement:
    effect: str  # "permit" | "forbid"
    action: str | None  # required action name, or None for "any"
    conditions_when: list[_Condition]
    conditions_unless: list[_Condition]

    def matches(self, ctx: RequestContext, action: str) -> bool:
        if self.action is not None and self.action != action:
            return False
        # when: every clause must hold
        if not all(c.holds(ctx) for c in self.conditions_when):
            return False
        # unless: if every clause holds, the exception fires -> no match
        if self.conditions_unless and all(c.holds(ctx) for c in self.conditions_unless):
            return False
        return True


# Cedar principal attribute -> RequestContext field name.
_ATTR_ALIASES = {"project": "project_id", "tenant": "tenant_id", "user": "user_id"}


def _principal_attr(ctx: RequestContext, attr: str) -> str | None:
    """Resolve ``principal.<attr>`` against the request context.

    ``role`` maps to the caller's roles (Cedar treats role as a scalar here,
    so a match against any held role counts). Other attributes map to the
    same-named context field, via ``_ATTR_ALIASES`` where they differ.
    """
    if attr == "role":
        # Handled specially by _Condition.holds so multi-role callers match.
        return None
    field = _ATTR_ALIASES.get(attr, attr)
    return getattr(ctx, field, None)


_ACTION_RE = re.compile(r'action\s*==\s*Action::"([^"]+)"')
_COND_RE = re.compile(r'principal\.(\w+)\s*(==|!=)\s*"([^"]+)"')
# The scope triple only — everything up to the closing paren of permit(...) /
# forbid(...). A when/unless body follows it and is parsed separately, so the
# rejections below must not see `principal.role == "senior"`.
_SCOPE_RE = re.compile(r"^(?:permit|forbid)\s*\((.*?)\)", re.DOTALL)
# Scope clauses the evaluator cannot honour. Left to the bare-variable path they
# would be dropped, and since each one *narrows* a statement, dropping it widens
# the statement's effect — fail-open. See the module docstring.
_UNSUPPORTED_SCOPE_RES = (
    re.compile(r"\bprincipal\s*(?:==|\bin\b)"),
    re.compile(r"\bresource\s*(?:==|\bin\b)"),
    re.compile(r"\baction\s+in\b"),
)


def _parse_conditions(clause: str) -> list[_Condition] | None:
    """Parse a ``when``/``unless`` body. Returns None if anything is unparseable."""
    conds: list[_Condition] = []
    for part in clause.split("&&"):
        part = part.strip()
        if not part:
            continue
        m = _COND_RE.search(part)
        if not m:
            return None
        attr, op, value = m.group(1), m.group(2), m.group(3)
        conds.append(_Condition(attr=attr, value=value, negated=(op == "!=")))
    return conds


def parse_policy(text: str) -> _Statement | None:
    """Parse a single Cedar statement. Returns None if unsupported."""
    text = text.strip().rstrip(";").strip()
    if text.startswith("permit"):
        effect = "permit"
    elif text.startswith("forbid"):
        effect = "forbid"
    else:
        return None

    scope_match = _SCOPE_RE.match(text)
    if scope_match is None:
        return None
    scope = scope_match.group(1)
    if any(pattern.search(scope) for pattern in _UNSUPPORTED_SCOPE_RES):
        return None

    action_match = _ACTION_RE.search(scope)
    action = action_match.group(1) if action_match else None

    when_when: list[_Condition] = []
    when_unless: list[_Condition] = []
    for keyword, target in (("when", "when"), ("unless", "unless")):
        m = re.search(keyword + r"\s*\{([^}]*)\}", text)
        if m:
            parsed = _parse_conditions(m.group(1))
            if parsed is None:
                return None
            if target == "when":
                when_when = parsed
            else:
                when_unless = parsed

    return _Statement(
        effect=effect,
        action=action,
        conditions_when=when_when,
        conditions_unless=when_unless,
    )


class CedarPolicyService:
    """Evaluates Cedar-subset policies for the AuthMiddleware PolicyService hook.

    Default-deny is scoped to the actions the policy set actually talks about.
    Textbook Cedar denies anything no ``permit`` covers, which is right when the
    whole policy set is authored before deployment. Here it is authored
    incrementally over HTTP, so a global default-deny makes the *first* policy an
    outage: ``permit(… Action::"read" …)`` says nothing about writes, and strict
    default-deny reads that silence as "forbid every write" — including
    ``POST /admin/policies``, so the second policy that would fix it cannot be
    submitted. Restarting is the only way out, and pre-fix policies did not
    survive a restart either.

    So an action is governed only once some enforcing statement mentions it.
    Within a governed action Cedar's rules are unchanged: a matching permit is
    required and a matching forbid overrides it. An ungoverned action falls
    through to the layers that are always on — authentication, admin RBAC, and
    quota enforcement — rather than to an unauthored deny.

    LOG_ONLY statements govern nothing, which is what makes the mode observable:
    ``POST /admin/policies`` defaults to LOG_ONLY, so the documented way to trial
    a policy must not be able to change any decision.
    """

    def __init__(self, policies: list[dict]) -> None:
        """Build from a list of policy dicts ({name, policy_text, mode, ...})."""
        self._statements: list[tuple[_Statement, dict]] = []
        # Actions some enforcing statement governs; None means "every action",
        # contributed by a statement with no action clause.
        self._governed: set[str | None] = set()
        self.reload(policies)

    def reload(self, policies: list[dict]) -> None:
        """Re-parse the policy set, replacing the compiled statements.

        Policies are parsed once rather than per request, so a policy written
        through ``POST /admin/policies`` would otherwise not take effect until a
        restart. The new lists are built first and swapped in at the end: an
        unparseable statement mid-list must not leave a half-built policy set
        deciding requests, and ``evaluate`` may run concurrently.
        """
        statements: list[tuple[_Statement, dict]] = []
        governed: set[str | None] = set()
        for policy in policies:
            text = policy.get("policy_text", "")
            stmt = parse_policy(text)
            if stmt is None:
                logger.warning(
                    "Skipping unparseable/unsupported policy %r", policy.get("name")
                )
                continue
            statements.append((stmt, policy))
            if policy.get("mode", "ENFORCE") != "LOG_ONLY":
                governed.add(stmt.action)
        self._statements = statements
        self._governed = governed

    def governs(self, cedar_action: str) -> bool:
        """Whether any enforcing statement claims authority over this action."""
        return None in self._governed or cedar_action in self._governed

    async def evaluate(
        self, context: RequestContext, action: str, resource: str
    ) -> str:
        """Return "ALLOW" or "DENY" for the request.

        ``action`` arrives as an HTTP method from the middleware; it is mapped to
        a Cedar action name before matching. Within a governed action: default
        deny, a permit is required, and any matching forbid overrides.
        """
        cedar_action = http_method_to_action(action)

        permitted = False
        for stmt, policy in self._statements:
            if not stmt.matches(context, cedar_action):
                continue
            # A LOG_ONLY policy is evaluated for observability but does not
            # affect the effective decision.
            if policy.get("mode", "ENFORCE") == "LOG_ONLY":
                logger.info(
                    "Policy %r (LOG_ONLY) would %s user=%s action=%s resource=%s",
                    policy.get("name"), stmt.effect, context.user_id, cedar_action, resource,
                )
                continue
            if stmt.effect == "forbid":
                return "DENY"  # forbid always wins
            permitted = True

        if permitted:
            return "ALLOW"
        if self.governs(cedar_action):
            return "DENY"  # authored default-deny
        # Nobody wrote a policy about this action; the other authorization
        # layers still apply.
        logger.debug(
            "No enforcing policy governs action=%s; deferring to auth/RBAC/quota "
            "(user=%s resource=%s)", cedar_action, context.user_id, resource,
        )
        return "ALLOW"
