"""A Cedar policy written over HTTP takes effect now and survives a restart.

`test_cedar_policy.py` asserts the evaluator's semantics; `test_admin_routes.py`
asserts the route's request/response shape. Neither covers what happens *between*
them, which is where the policy path was broken in two ways at once:

  * Statements are compiled in `CedarPolicyService.__init__`, so a policy added
    through `POST /admin/policies` landed in the list without affecting a single
    request until the process was restarted.
  * Nothing persisted it, so the restart that would have applied it also lost it.

Together those made the admin policy API write-only in effect. These tests drive
the real route against the real evaluator and the real `DynamoPersistence`
serializers, so a regression in either half fails here rather than in production.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import AuthMethod, RequestContext
from src.gateway.persistence import DynamoPersistence

PERMIT_READ = 'permit(principal, action == Action::"read", resource);'
PERMIT_WRITE = 'permit(principal, action == Action::"write", resource);'
FORBID_WRITE = 'forbid(principal, action == Action::"write", resource);'


def _ctx(roles=None, project="proj-alpha") -> RequestContext:
    return RequestContext(
        user_id="u1",
        project_id=project,
        roles=roles or [],
        scopes=[],
        auth_method=AuthMethod.API_KEY,
    )


def _decide(service: CedarPolicyService, method: str, path: str = "/x") -> str:
    return asyncio.run(service.evaluate(_ctx(), method, path))


class _RecordingPersistence(DynamoPersistence):
    """The real persistence class with the Dynamo call captured.

    Subclassed rather than faked so `serialize_cedar_policy` — the part that
    decides what actually reaches the table — is the code under test. Only the
    `put_item` boundary is replaced.
    """

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self.written: list[dict] = []

    def _get_table(self):
        recorder = self

        class _Table:
            def put_item(self, Item):  # noqa: N803 — boto3's parameter name
                recorder.written.append(Item)

        return _Table()


@pytest.fixture
def wired(monkeypatch):
    """An admin API sharing one policy list and one evaluator, as bootstrap wires it."""
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    policies: list[dict] = []
    service = CedarPolicyService(policies)
    persistence = _RecordingPersistence()
    admin_api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        policies=policies,
        policy_service=service,
        persistence=persistence,
    )
    client = TestClient(Starlette(routes=create_admin_routes(admin_api)))
    return client, service, persistence, admin_api


def _post(client: TestClient, name: str, text: str, mode: str = "ENFORCE"):
    return client.post(
        "/admin/policies", json={"name": name, "policy_text": text, "mode": mode}
    )


class TestAPolicyTakesEffectWithoutARestart:
    def test_a_new_forbid_starts_denying_immediately(self, wired):
        client, service, _, _ = wired
        assert _decide(service, "post") == "ALLOW", "no policy governs write yet"

        assert _post(client, "no-writes", FORBID_WRITE).status_code == 201

        assert _decide(service, "post") == "DENY", (
            "the evaluator still holds statements compiled before the POST — "
            "create_policy must reload it"
        )
        assert _decide(service, "get") == "ALLOW", "the forbid only names write"

    def test_updating_a_policy_replaces_the_compiled_statement(self, wired):
        """Not just appends — otherwise the superseded statement keeps deciding,
        and for a permit replaced by a forbid the stale one is the permissive."""
        client, service, _, _ = wired
        _post(client, "writes", PERMIT_WRITE)
        assert _decide(service, "post") == "ALLOW"

        assert _post(client, "writes", FORBID_WRITE).status_code == 200
        assert _decide(service, "post") == "DENY"

    def test_a_log_only_policy_changes_no_decision(self, wired):
        """The route's default mode. Adding one must be observably inert — that
        is the whole point of trialling a policy before enforcing it."""
        client, service, _, _ = wired
        assert _post(client, "trial", FORBID_WRITE, mode="LOG_ONLY").status_code == 201

        for method in ("get", "post", "put", "patch", "delete", "head"):
            assert _decide(service, method) == "ALLOW", method

    def test_promoting_a_trial_policy_to_enforce_starts_denying(self, wired):
        """The documented workflow end to end: trial in LOG_ONLY, then enforce."""
        client, service, _, _ = wired
        _post(client, "trial", FORBID_WRITE, mode="LOG_ONLY")
        assert _decide(service, "post") == "ALLOW"

        _post(client, "trial", FORBID_WRITE, mode="ENFORCE")
        assert _decide(service, "post") == "DENY"

    def test_the_first_read_policy_does_not_lock_out_the_policy_api(self, wired):
        """The bug that motivated the change, as an end-to-end sequence.

        A read-permit used to make every write a DENY, including the POST that
        would add the balancing write-permit — so the gateway could not be
        recovered through its own API.
        """
        client, service, _, _ = wired
        _post(client, "reads", PERMIT_READ)

        assert _decide(service, "post", "/admin/policies") == "ALLOW"
        assert _post(client, "writes", PERMIT_WRITE).status_code == 201
        assert _decide(service, "post", "/api/chat") == "ALLOW"

    def test_a_rejected_policy_leaves_the_evaluator_untouched(self, wired):
        """400 means nothing changed — not that the evaluator was rebuilt from a
        list containing text it will silently skip."""
        client, service, persistence, admin_api = wired
        _post(client, "no-writes", FORBID_WRITE)

        assert _post(client, "bad", "not cedar at all").status_code == 400

        assert _decide(service, "post") == "DENY"
        assert [p["name"] for p in admin_api.policies] == ["no-writes"]
        assert [i["name"] for i in persistence.written] == ["no-writes"]


class TestAPolicySurvivesARestart:
    def test_the_policy_is_written_to_persistence(self, wired):
        client, _, persistence, _ = wired
        _post(client, "no-writes", FORBID_WRITE)

        assert len(persistence.written) == 1
        item = persistence.written[0]
        assert item["PK"] == "CEDAR_POLICY#no-writes"
        assert item["SK"] == "CONFIG"
        assert item["entity_type"] == "cedar_policy"
        assert item["policy_text"] == FORBID_WRITE
        assert item["mode"] == "ENFORCE"

    def test_an_update_overwrites_rather_than_duplicating(self, wired):
        """Same name, same key — so a restart reloads one statement, not two
        contradictory ones whose forbid would win."""
        client, _, persistence, _ = wired
        _post(client, "writes", PERMIT_WRITE)
        _post(client, "writes", FORBID_WRITE)

        keys = {i["PK"] for i in persistence.written}
        assert keys == {"CEDAR_POLICY#writes"}
        assert persistence.written[-1]["policy_text"] == FORBID_WRITE

    def test_a_round_trip_through_the_serializers_decides_the_same_way(self, wired):
        """What comes back out of the table must evaluate identically to what
        went in — the restart half of the guarantee."""
        client, service, persistence, _ = wired
        _post(client, "no-writes", FORBID_WRITE)
        _post(client, "reads", PERMIT_READ, mode="LOG_ONLY")

        restored = [
            DynamoPersistence.deserialize_cedar_policy(item) for item in persistence.written
        ]
        rebuilt = CedarPolicyService(restored)
        for method in ("get", "post", "put", "delete"):
            assert _decide(rebuilt, method) == _decide(service, method), method

    def test_a_stored_item_missing_mode_is_not_enforcing(self, wired):
        """An item written before `mode` existed, or truncated, must come back
        LOG_ONLY. Defaulting a malformed policy to ENFORCE would let a partial
        write start denying traffic."""
        restored = DynamoPersistence.deserialize_cedar_policy(
            {"name": "legacy", "policy_text": FORBID_WRITE}
        )
        assert restored["mode"] == "LOG_ONLY"
        assert _decide(CedarPolicyService([restored]), "post") == "ALLOW"

    def test_persistence_disabled_is_a_no_op_not_an_error(self, monkeypatch):
        """The local clean-install case: the route still works, the policy still
        applies to this process, and nothing pretends it was saved."""
        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        persistence = DynamoPersistence()
        assert not persistence.enabled
        policies: list[dict] = []
        service = CedarPolicyService(policies)
        client = TestClient(
            Starlette(
                routes=create_admin_routes(
                    AdminAPI(
                        cost_tracker=CostTracker(pricing_config={}),
                        health_tracker=ProviderHealthTracker(),
                        model_registry=ModelRegistry(),
                        policies=policies,
                        policy_service=service,
                        persistence=persistence,
                    )
                )
            )
        )

        assert _post(client, "no-writes", FORBID_WRITE).status_code == 201
        assert _decide(service, "post") == "DENY"
        assert asyncio.run(persistence.load_all_cedar_policies()) == []


class TestWiringWithoutAnEvaluator:
    def test_create_policy_works_when_no_policy_service_is_wired(self):
        """`policy_service` and `persistence` are both optional — an AdminAPI
        built without them (the agent-only bootstrap, or a test) must still store
        the policy rather than 500 on the reload or the save."""
        admin_api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}),
            health_tracker=ProviderHealthTracker(),
            model_registry=ModelRegistry(),
        )
        client = TestClient(Starlette(routes=create_admin_routes(admin_api)))

        assert _post(client, "no-writes", FORBID_WRITE).status_code == 201
        assert [p["name"] for p in admin_api.policies] == ["no-writes"]
