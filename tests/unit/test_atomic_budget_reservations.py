"""Atomic and idempotent budget reservation contracts."""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from boto3.dynamodb.types import TypeDeserializer

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionResponse,
    ProviderModelMapping,
    RateLimitResult,
    RequestContext,
    ResolvedPolicy,
    TokenPricing,
    TokenUsage,
)
from src.gateway.persistence import DynamoPersistence
from src.gateway.quota_enforcer import (
    BudgetReservation,
    QuotaDecision,
    QuotaEnforcer,
)


class _TransactionCanceled(RuntimeError):
    def __init__(self, item_count: int, failed_index: int) -> None:
        reasons = [{"Code": "None"} for _ in range(item_count)]
        reasons[failed_index] = {"Code": "ConditionalCheckFailed"}
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        }
        super().__init__("transaction condition failed")


class _TransactionalClient:
    """All-or-nothing interpreter for budget reservation transactions."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self._decoder = TypeDeserializer()
        self.fail_finalize_once = False

    def _decode(self, values: dict) -> dict:
        return {
            name: self._decoder.deserialize(value)
            for name, value in values.items()
        }

    @staticmethod
    def _key(item: dict) -> tuple[str, str]:
        return item["PK"], item["SK"]

    def transact_write_items(self, **request) -> None:
        with self._lock:
            operations = request["TransactItems"]
            if (
                self.fail_finalize_once
                and operations
                and operations[0].get("Update", {})
                .get("UpdateExpression", "")
                .startswith("SET #state")
            ):
                self.fail_finalize_once = False
                raise RuntimeError("injected finalization outage")
            staged = copy.deepcopy(self.rows)
            for index, operation in enumerate(operations):
                if "Put" in operation:
                    item = self._decode(operation["Put"]["Item"])
                    key = self._key(item)
                    if key in staged:
                        raise _TransactionCanceled(
                            len(operations),
                            index,
                        )
                    staged[key] = item
                    continue

                update = operation["Update"]
                key = self._key(self._decode(update["Key"]))
                values = self._decode(
                    update["ExpressionAttributeValues"]
                )
                expression = update["UpdateExpression"]
                current = copy.deepcopy(
                    staged.get(
                        key,
                        {"PK": key[0], "SK": key[1]},
                    )
                )
                if expression.startswith("SET entity_type"):
                    current["entity_type"] = values[":entity_type"]
                    current["budget_scope"] = values[":scope"]
                    current["updated_at"] = values[":updated_at"]
                    if ":amount" in values:
                        spend = current.get("spend")
                        epoch = current.get("epoch", 0)
                        if (
                            epoch != values[":expected_epoch"]
                            or (
                                spend is not None
                                and spend > values[":max_before"]
                            )
                        ):
                            raise _TransactionCanceled(
                                len(operations),
                                index,
                            )
                        current["epoch"] = epoch
                        current["spend"] = (
                            current.get("spend", 0) + values[":amount"]
                        )
                    else:
                        current["spend"] = values[":zero"]
                        current["epoch"] = (
                            current.get("epoch", 0) + values[":one"]
                        )
                elif expression.startswith("SET #state"):
                    if (
                        current.get("state")
                        != values[":reserved_state"]
                        or current.get("reservation_signature")
                        != values[":signature"]
                        or current.get("settlement_state")
                        != values[":reported"]
                        or current.get("settlement_amount")
                        != values[":actual"]
                    ):
                        raise _TransactionCanceled(
                            len(operations),
                            index,
                        )
                    current["state"] = values[":finalized_state"]
                    current["actual_cost"] = values[":actual"]
                    current["updated_at"] = values[":updated_at"]
                    current["expires_at"] = values[":expires_at"]
                else:
                    assert expression.startswith("SET updated_at")
                    if (
                        "spend" not in current
                        or current["spend"] < values[":reserved"]
                        or current.get("epoch", 0)
                        != values[":expected_epoch"]
                    ):
                        raise _TransactionCanceled(
                            len(operations),
                            index,
                        )
                    current["updated_at"] = values[":updated_at"]
                    current["epoch"] = current.get("epoch", values[":zero"])
                    current["spend"] += values[":delta"]
                staged[key] = current
            self.rows = staged


class _Table:
    def __init__(self, client: _TransactionalClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True
        item = self._client.rows.get((Key["PK"], Key["SK"]))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def update_item(
        self,
        *,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ConditionExpression,  # noqa: N803
        ExpressionAttributeNames,  # noqa: N803
        ExpressionAttributeValues,  # noqa: N803
    ):
        assert UpdateExpression.startswith("SET settlement_amount")
        assert "#state" in ExpressionAttributeNames
        assert "settlement_state" in ConditionExpression
        with self._client._lock:
            storage_key = (Key["PK"], Key["SK"])
            current = self._client.rows.get(storage_key)
            values = ExpressionAttributeValues
            if (
                current is None
                or current.get("state") != values[":reserved_state"]
                or current.get("reservation_signature")
                != values[":signature"]
                or (
                    current.get("settlement_state") == "reported"
                    and current.get("settlement_amount")
                    != values[":actual"]
                )
            ):
                raise _TransactionCanceled(1, 0)
            current["settlement_amount"] = values[":actual"]
            current["settlement_state"] = values[":reported"]
            current["updated_at"] = values[":updated_at"]
        return {}

    def query(
        self,
        KeyConditionExpression,  # noqa: N803
        ConsistentRead=False,  # noqa: N803
        ExclusiveStartKey=None,  # noqa: N803
    ):
        assert ConsistentRead is True
        expressions = KeyConditionExpression.get_expression()["values"]
        partition = expressions[0].get_expression()["values"][1]
        prefix = expressions[1].get_expression()["values"][1]
        return {
            "Items": [
                copy.deepcopy(item)
                for (pk, sk), item in self._client.rows.items()
                if pk == partition and sk.startswith(prefix)
            ]
        }


class _Persistence(DynamoPersistence):
    def __init__(self, client: _TransactionalClient) -> None:
        super().__init__()
        self._enabled = True
        self._table = _Table(client)

    def _get_table(self):
        return self._table


def _run(coro):
    return asyncio.run(coro)


def _counter(client, scope: str, ident: str) -> float:
    row = client.rows[(f"SPEND#{scope}#{ident}", "TOTAL")]
    return float(row["spend"])


def test_concurrent_requests_cannot_both_spend_the_last_capacity():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]

    async def reserve(request_id: str):
        return await store.reserve_budget(
            request_id=request_id,
            reservations=counters,
            amount=60.0,
        )

    async def race():
        return await asyncio.gather(
            reserve("request-a"),
            reserve("request-b"),
        )

    first, second = _run(race())

    assert sorted([first.allowed, second.allowed]) == [False, True]
    assert _counter(client, "quota", "tenant-a/project-a") == 60.0


def test_duplicate_reservation_and_finalization_charge_once():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]

    first = _run(store.reserve_budget(
        request_id="request-a",
        reservations=counters,
        amount=60.0,
    ))
    duplicate = _run(store.reserve_budget(
        request_id="request-a",
        reservations=counters,
        amount=60.0,
    ))

    assert first.allowed
    assert duplicate.allowed and duplicate.idempotent
    assert _counter(client, "quota", "tenant-a/project-a") == 60.0

    finalized = _run(store.finalize_budget_reservation(
        request_id="request-a",
        reservations=counters,
        reserved_amount=60.0,
        actual_cost=40.0,
    ))
    repeated = _run(store.finalize_budget_reservation(
        request_id="request-a",
        reservations=counters,
        reserved_amount=60.0,
        actual_cost=40.0,
    ))

    assert finalized.state == "finalized"
    assert repeated.idempotent
    assert _counter(client, "quota", "tenant-a/project-a") == 40.0


def test_finalization_accounts_actual_cost_above_the_estimate():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]

    _run(store.reserve_budget(
        request_id="request-a",
        reservations=counters,
        amount=40.0,
    ))
    result = _run(store.finalize_budget_reservation(
        request_id="request-a",
        reservations=counters,
        reserved_amount=40.0,
        actual_cost=55.0,
    ))

    assert result.totals["quota"] == 55.0
    assert _counter(client, "quota", "tenant-a/project-a") == 55.0


def test_multi_scope_denial_rolls_back_every_counter_and_marker():
    client = _TransactionalClient()
    store = _Persistence(client)
    user_key = ("SPEND#user#tenant-a/user-a", "TOTAL")
    client.rows[user_key] = {
        "PK": user_key[0],
        "SK": user_key[1],
        "spend": 45,
    }
    counters = [
        ("quota", "tenant-a/project-a", 100.0),
        ("user", "tenant-a/user-a", 50.0),
    ]

    result = _run(store.reserve_budget(
        request_id="request-a",
        reservations=counters,
        amount=10.0,
    ))

    assert not result.allowed
    assert result.denied_scope == "user"
    assert ("SPEND#quota#tenant-a/project-a", "TOTAL") not in client.rows
    assert _counter(client, "user", "tenant-a/user-a") == 45.0
    assert not any(
        key[1].startswith("RESERVATION#")
        for key in client.rows
    )


def test_tenant_qualified_counters_do_not_share_capacity():
    client = _TransactionalClient()
    store = _Persistence(client)

    for tenant in ("tenant-a", "tenant-b"):
        result = _run(store.reserve_budget(
            request_id=f"request-{tenant}",
            reservations=[
                ("quota", f"{tenant}/same-project", 50.0)
            ],
            amount=40.0,
        ))
        assert result.allowed

    assert _counter(client, "quota", "tenant-a/same-project") == 40.0
    assert _counter(client, "quota", "tenant-b/same-project") == 40.0


def test_expired_reservation_keeps_estimate_when_no_settlement_was_reported():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]
    started = datetime(2026, 8, 7, tzinfo=timezone.utc)

    _run(store.reserve_budget(
        request_id="abandoned-request",
        reservations=counters,
        amount=60.0,
        now=started,
        lease_seconds=60,
    ))
    marker = next(
        row
        for (_pk, sk), row in client.rows.items()
        if sk.startswith("RESERVATION#")
    )
    assert "expires_at" not in marker

    released = _run(store.release_expired_budget_reservations(
        primary_scope="quota",
        primary_ident="tenant-a/project-a",
        now=started + timedelta(seconds=61),
    ))

    assert released == 1
    assert _counter(client, "quota", "tenant-a/project-a") == 60.0
    assert marker["state"] == "reserved"
    stored_marker = next(
        row
        for (_pk, sk), row in client.rows.items()
        if sk.startswith("RESERVATION#")
    )
    assert stored_marker["state"] == "finalized"
    assert float(stored_marker["actual_cost"]) == 60.0
    assert "expires_at" in stored_marker


def test_explicit_release_records_zero_for_expiry_reconciliation():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]

    _run(store.reserve_budget(
        request_id="rejected-request",
        reservations=counters,
        amount=60.0,
    ))
    result = _run(store.release_budget_reservation(
        request_id="rejected-request",
        reservations=counters,
        reserved_amount=60.0,
    ))

    assert result is not None
    assert _counter(client, "quota", "tenant-a/project-a") == 0.0


def test_reset_starts_new_epoch_without_old_reservation_charging_it():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]

    reserved = _run(store.reserve_budget(
        request_id="old-cycle-request",
        reservations=counters,
        amount=60.0,
    ))
    states = _run(store.reset_spend_counters([
        ("quota", "tenant-a/project-a"),
        ("project", "tenant-a/project-a"),
    ]))
    finalized = _run(store.finalize_budget_reservation(
        request_id="old-cycle-request",
        reservations=counters,
        reserved_amount=60.0,
        actual_cost=40.0,
    ))

    assert reserved.epochs == {"quota": 0}
    assert states["quota"].epoch == 1
    assert states["project"].epoch == 1
    assert finalized.totals["quota"] == 0.0
    assert finalized.epochs["quota"] == 1
    assert _counter(client, "quota", "tenant-a/project-a") == 0.0

    next_cycle = _run(store.reserve_budget(
        request_id="new-cycle-request",
        reservations=counters,
        amount=70.0,
    ))
    assert next_cycle.allowed
    assert next_cycle.epochs == {"quota": 1}
    assert _counter(client, "quota", "tenant-a/project-a") == 70.0


def test_project_reset_does_not_discard_same_request_user_spend():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [
        ("quota", "tenant-a/project-a", 100.0),
        ("user", "tenant-a/user-a", 80.0),
    ]

    _run(store.reserve_budget(
        request_id="cross-cycle-request",
        reservations=counters,
        amount=60.0,
    ))
    _run(store.reset_spend_counters([
        ("quota", "tenant-a/project-a"),
        ("project", "tenant-a/project-a"),
    ]))
    result = _run(store.finalize_budget_reservation(
        request_id="cross-cycle-request",
        reservations=counters,
        reserved_amount=60.0,
        actual_cost=40.0,
    ))

    assert result.totals == {"quota": 0.0, "user": 40.0}
    assert _counter(client, "quota", "tenant-a/project-a") == 0.0
    assert _counter(client, "user", "tenant-a/user-a") == 40.0


def test_failed_finalization_cleanup_uses_reported_actual_cost():
    client = _TransactionalClient()
    store = _Persistence(client)
    counters = [("quota", "tenant-a/project-a", 100.0)]
    started = datetime(2026, 8, 7, tzinfo=timezone.utc)

    _run(store.reserve_budget(
        request_id="provider-completed",
        reservations=counters,
        amount=60.0,
        now=started,
        lease_seconds=60,
    ))
    client.fail_finalize_once = True
    failed = _run(store.finalize_budget_reservation(
        request_id="provider-completed",
        reservations=counters,
        reserved_amount=60.0,
        actual_cost=40.0,
        now=started + timedelta(seconds=30),
    ))
    marker = next(
        row
        for (_pk, sk), row in client.rows.items()
        if sk.startswith("RESERVATION#")
    )

    assert failed is None
    assert marker["state"] == "reserved"
    assert float(marker["settlement_amount"]) == 40.0
    assert marker["settlement_state"] == "reported"

    reconciled = _run(store.release_expired_budget_reservations(
        primary_scope="quota",
        primary_ident="tenant-a/project-a",
        now=started + timedelta(seconds=61),
    ))
    assert reconciled == 1
    assert _counter(client, "quota", "tenant-a/project-a") == 40.0


def test_reset_epoch_unblocks_stale_quota_and_cost_tracker_instances():
    client = _TransactionalClient()
    store = _Persistence(client)
    project_ident = "tenant:8:tenant-a:project:9:project-a"
    client.rows[(f"SPEND#quota#{project_ident}", "TOTAL")] = {
        "PK": f"SPEND#quota#{project_ident}",
        "SK": "TOTAL",
        "spend": 120,
        "epoch": 0,
    }
    client.rows[(f"SPEND#project#{project_ident}", "TOTAL")] = {
        "PK": f"SPEND#project#{project_ident}",
        "SK": "TOTAL",
        "spend": 120,
        "epoch": 0,
    }
    quota = QuotaEnforcer(persistence=store)
    tracker = CostTracker(pricing_config={}, persistence=store)
    tracker.register_project(
        "project-a",
        budget_limit=100.0,
        tenant_id="tenant-a",
    )

    assert _run(quota.current_spend(
        "project-a",
        tenant_id="tenant-a",
    )) == 120.0
    assert _run(tracker.check_budget(
        "project-a",
        tenant_id="tenant-a",
    )).is_over_budget

    assert _run(quota.reset_spend(
        "project-a",
        tenant_id="tenant-a",
    ))
    decision = _run(quota.enforce_all(
        project_id="project-a",
        model="model-a",
        provider=None,
        max_tokens=None,
        estimated_cost=1.0,
        policy=ResolvedPolicy(budget_limit=100.0),
        tenant_id="tenant-a",
    ))
    status = _run(tracker.check_budget(
        "project-a",
        tenant_id="tenant-a",
    ))

    assert decision.allowed
    assert status.current_spend == 0.0
    assert not status.is_over_budget


def test_reserved_budget_finalization_alerts_once_per_epoch():
    client = _TransactionalClient()
    store = _Persistence(client)
    first = QuotaEnforcer(persistence=store)
    second = QuotaEnforcer(persistence=store)
    alerts: list[tuple[float, str | None, int]] = []
    for enforcer in (first, second):
        enforcer.on_budget_alert(
            lambda project, threshold, spend, limit, tenant, epoch: (
                alerts.append((threshold, tenant, epoch))
            )
        )

    decision = _run(first.reserve_budget(
        request_id="threshold-request",
        project_id="project-a",
        user_id="user-a",
        estimated_cost=85.0,
        project_budget_limit=100.0,
        user_budget_limit=None,
        tenant_id="tenant-a",
    ))
    _run(first.finalize_budget(
        decision.reservation,
        85.0,
        tenant_id="tenant-a",
        project_id="project-a",
    ))
    _run(second.finalize_budget(
        decision.reservation,
        85.0,
        tenant_id="tenant-a",
        project_id="project-a",
    ))

    assert alerts == [(0.8, "tenant-a", 0)]

    _run(first.reset_spend("project-a", tenant_id="tenant-a"))
    next_cycle = _run(second.reserve_budget(
        request_id="next-threshold-request",
        project_id="project-a",
        user_id="user-a",
        estimated_cost=85.0,
        project_budget_limit=100.0,
        user_budget_limit=None,
        tenant_id="tenant-a",
    ))
    _run(second.finalize_budget(
        next_cycle.reservation,
        85.0,
        tenant_id="tenant-a",
        project_id="project-a",
    ))

    assert alerts == [
        (0.8, "tenant-a", 0),
        (0.8, "tenant-a", 1),
    ]


def test_two_enforcers_share_one_atomic_admission_gate():
    client = _TransactionalClient()
    store = _Persistence(client)
    first = QuotaEnforcer(persistence=store)
    second = QuotaEnforcer(persistence=store)

    async def race():
        return await asyncio.gather(
            first.reserve_budget(
                request_id="request-a",
                project_id="project-a",
                user_id="user-a",
                estimated_cost=60.0,
                project_budget_limit=100.0,
                user_budget_limit=None,
                tenant_id="tenant-a",
            ),
            second.reserve_budget(
                request_id="request-b",
                project_id="project-a",
                user_id="user-b",
                estimated_cost=60.0,
                project_budget_limit=100.0,
                user_budget_limit=None,
                tenant_id="tenant-a",
            ),
        )

    decisions = _run(race())
    assert sum(decision.allowed for decision in decisions) == 1
    winner = next(decision for decision in decisions if decision.allowed)
    totals = _run(first.finalize_budget(
        winner.reservation,
        55.0,
        tenant_id="tenant-a",
        project_id="project-a",
    ))

    assert totals["quota"] == 55.0


def test_agent_reserves_before_dispatch_and_finalizes_actual_cost():
    events: list[str] = []
    router = MagicMock()
    router._smart_strategy = None
    router._ensemble_config = None
    router.get_fallback_chain.return_value = [
        ProviderModelMapping(provider="openai", model_id="gpt-4")
    ]

    async def execute(*_args, **_kwargs):
        events.append("provider")
        return ChatCompletionResponse(
            id="response-a",
            choices=[{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
            }],
            usage=TokenUsage(10, 5, 15),
            model="gpt-4",
            provider="openai",
        )

    router.execute_with_fallback = execute
    rate_limiter = MagicMock()
    rate_limiter.check_rate_limit = AsyncMock(return_value=RateLimitResult(
        allowed=True,
        limit=60,
        remaining=59,
        reset_at=datetime.now(timezone.utc),
    ))
    reservation = BudgetReservation(
        request_id="placeholder",
        counters=(("quota", "proj-1", 100.0),),
        amount=0.0003,
    )
    quota = MagicMock(spec=QuotaEnforcer)
    quota.enforce_all = AsyncMock(return_value=QuotaDecision(allowed=True))
    quota.cap_max_tokens.side_effect = lambda value, _policy: value

    async def reserve(**kwargs):
        events.append("reserve")
        return QuotaDecision(
            allowed=True,
            reservation=BudgetReservation(
                request_id=kwargs["request_id"],
                counters=reservation.counters,
                amount=kwargs["estimated_cost"],
            ),
        )

    async def finalize(_reservation, actual_cost, **_kwargs):
        events.append("finalize")
        assert actual_cost == 0.00025
        return {"quota": actual_cost}

    quota.reserve_budget = reserve
    quota.finalize_budget = finalize
    quota.record_spend = AsyncMock()
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        return_value=ResolvedPolicy(budget_limit=100.0)
    )
    tracker = CostTracker(pricing_config={
        "openai": {
            "gpt-4": TokenPricing(
                prompt_token_cost=0.01,
                completion_token_cost=0.03,
            )
        }
    })
    agent = GatewayAgent(
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=tracker,
        quota_enforcer=quota,
        policy_resolver=resolver,
    )

    result = _run(agent.handle_chat_completion(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
        },
        {
            "user_id": "user-1",
            "project_id": "proj-1",
            "roles": ["tenant_member"],
            "scopes": ["chat"],
        },
    ))

    assert result["id"] == "response-a"
    assert events == ["reserve", "provider", "finalize"]
    quota.record_spend.assert_not_awaited()


def test_abandoned_stream_reconciles_its_reservation() -> None:
    quota = MagicMock(spec=QuotaEnforcer)
    quota.finalize_budget = AsyncMock(
        return_value={"quota": 0.25, "user": 0.25}
    )
    tracker = MagicMock(spec=CostTracker)
    agent = GatewayAgent(
        router=MagicMock(),
        rate_limiter=MagicMock(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=tracker,
        quota_enforcer=quota,
    )
    reservation = BudgetReservation(
        request_id="stream-a",
        counters=(("quota", "project-a", 10.0),),
        amount=0.25,
    )
    context = RequestContext(
        user_id="user-a",
        project_id="project-a",
        roles=[],
        scopes=[],
        tenant_id="tenant-a",
    )
    inner_closed = False

    async def inner():
        nonlocal inner_closed
        try:
            yield {"data": "started"}
            await asyncio.Event().wait()
        finally:
            inner_closed = True

    async def abandon():
        guarded = agent._guard_budgeted_stream(
            inner(),
            reservation,
            req_ctx=context,
        )
        assert await anext(guarded) == {"data": "started"}
        await guarded.aclose()

    _run(abandon())

    assert inner_closed is True
    quota.finalize_budget.assert_awaited_once_with(
        reservation,
        0.25,
        tenant_id="tenant-a",
        project_id="project-a",
    )
    tracker.adopt_user_spend.assert_called_once_with(
        "user-a",
        0.25,
        tenant_id="tenant-a",
    )


def test_abandoned_stream_does_not_repeat_inner_settlement() -> None:
    quota = MagicMock(spec=QuotaEnforcer)
    quota.finalize_budget = AsyncMock(
        return_value={"quota": 0.1, "user": 0.1}
    )
    agent = GatewayAgent(
        router=MagicMock(),
        rate_limiter=MagicMock(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=MagicMock(spec=CostTracker),
        quota_enforcer=quota,
    )
    reservation = BudgetReservation(
        request_id="stream-inner-finalized",
        counters=(("quota", "project-a", 10.0),),
        amount=0.5,
    )
    context = RequestContext(
        user_id="user-a",
        project_id="project-a",
        roles=[],
        scopes=[],
        tenant_id="tenant-a",
    )

    async def inner():
        try:
            yield {"data": "started"}
            await asyncio.Event().wait()
        finally:
            await agent._finalize_request_budget(
                reservation,
                actual_cost=0.1,
                req_ctx=context,
            )

    async def abandon():
        guarded = agent._guard_budgeted_stream(
            inner(),
            reservation,
            req_ctx=context,
        )
        assert await anext(guarded) == {"data": "started"}
        await guarded.aclose()

    _run(abandon())

    quota.finalize_budget.assert_awaited_once_with(
        reservation,
        0.1,
        tenant_id="tenant-a",
        project_id="project-a",
    )
    assert (
        reservation.request_id
        not in agent._budget_stream_settlements
    )


def test_cancelled_stream_before_first_event_reconciles_reservation() -> None:
    quota = MagicMock(spec=QuotaEnforcer)
    quota.finalize_budget = AsyncMock(return_value={"quota": 0.5})
    agent = GatewayAgent(
        router=MagicMock(),
        rate_limiter=MagicMock(),
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=MagicMock(spec=CostTracker),
        quota_enforcer=quota,
    )
    reservation = BudgetReservation(
        request_id="stream-b",
        counters=(("quota", "project-a", 10.0),),
        amount=0.5,
    )
    context = RequestContext(
        user_id="user-a",
        project_id="project-a",
        roles=[],
        scopes=[],
        tenant_id="tenant-a",
    )

    async def cancel_before_first_event():
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def inner():
            started.set()
            await blocked.wait()
            yield {"data": "never"}

        guarded = agent._guard_budgeted_stream(
            inner(),
            reservation,
            req_ctx=context,
        )
        pending = asyncio.create_task(anext(guarded))
        await started.wait()
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("stream cancellation was suppressed")

    _run(cancel_before_first_event())

    quota.finalize_budget.assert_awaited_once_with(
        reservation,
        0.5,
        tenant_id="tenant-a",
        project_id="project-a",
    )
