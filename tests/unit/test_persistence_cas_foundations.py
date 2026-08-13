"""Focused concurrency tests for persistence and config-sync CAS foundations."""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.config_sync import ConfigSyncService
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    PolicyNode,
    Principal,
    Project,
    ScimUser,
    TenantRole,
)
from src.gateway.persistence import (
    CanonicalMembershipConflictError,
    DynamoPersistence,
    PersistenceConflictError,
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


class _ConditionalCheckFailed(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException"},
        }
        super().__init__("conditional write failed")


class _CasDynamoClient:
    """All-or-nothing interpreter for the CAS transaction shapes under test."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[dict[str, Any]] = []
        self.fail_at: int | None = None
        self.before_transaction: Callable[["_CasDynamoClient"], None] | None = None
        self.after_query_snapshot: (
            Callable[["_CasDynamoClient"], None] | None
        ) = None
        self.query_count = 0
        self._lock = threading.RLock()
        self._deserializer = TypeDeserializer()

    def decode(self, values: dict[str, Any]) -> dict[str, Any]:
        return {
            name: self._deserializer.deserialize(value)
            for name, value in values.items()
        }

    @staticmethod
    def key(item: dict[str, Any]) -> tuple[str, str]:
        return str(item["PK"]), str(item["SK"])

    @staticmethod
    def _condition_holds(
        expression: str | None,
        current: dict[str, Any] | None,
        values: dict[str, Any],
    ) -> bool:
        if expression is None:
            return True

        missing_key_clause = (
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        )
        if current is None:
            return missing_key_clause in expression
        if expression == missing_key_clause:
            return False

        identity_tokens = {
            ":entity_type": "entity_type",
            ":node_type": "entity_type",
            ":tenant_id": "tenant_id",
            ":project_id": "project_id",
            ":user_id": "user_id",
        }
        for token, field in identity_tokens.items():
            if token in values and f"{field} = {token}" in expression:
                if current.get(field) != values[token]:
                    return False
        if (
            ":expected_schema" in values
            and current.get("schema_version")
            != values[":expected_schema"]
        ):
            return False

        if expression == "authorization_version = :expected":
            return (
                current.get("authorization_version")
                == values[":expected"]
            )

        if "#revision" in expression:
            expected_token = (
                ":expected_revision"
                if ":expected_revision" in values
                else ":expected"
            )
            expected = values[expected_token]
            if "revision" not in current:
                return (
                    "attribute_not_exists(#revision)" in expression
                    and expected == 0
                )
            return current["revision"] == expected

        return True

    @staticmethod
    def _apply_update(
        update: dict[str, Any],
        current: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        changed = copy.deepcopy(current)
        expression = update["UpdateExpression"]

        if expression == "ADD #version :one":
            changed["version"] = (
                changed.get("version", 0) + values[":one"]
            )
            return changed

        if expression.startswith("SET #revision = :next"):
            changed.update(
                {
                    "revision": values[":next"],
                    "entity_type": values[":entity_type"],
                    "tenant_id": values[":tenant_id"],
                    "updated_at": values[":updated_at"],
                }
            )
            return changed

        if expression.startswith("SET entity_type = :entity_type"):
            changed["entity_type"] = values[":entity_type"]
            changed["tenant_id"] = values[":tenant_id"]
            changed["version"] = (
                changed.get("version", 0) + values[":one"]
            )
            return changed

        raise AssertionError(f"unsupported update expression: {expression}")

    def transact_write_items(self, **request: Any) -> None:
        with self._lock:
            self.transactions.append(copy.deepcopy(request))
            hook = self.before_transaction
            self.before_transaction = None
            if hook is not None:
                hook(self)

            operations = request["TransactItems"]
            staged = copy.deepcopy(self.rows)
            for index, operation in enumerate(operations):
                if self.fail_at == index:
                    raise RuntimeError(f"injected transaction failure at {index}")

                if "Put" in operation:
                    put = operation["Put"]
                    item = self.decode(put["Item"])
                    key = self.key(item)
                    current = staged.get(key)
                    values = self.decode(
                        put.get("ExpressionAttributeValues", {})
                    )
                    if not self._condition_holds(
                        put.get("ConditionExpression"),
                        current,
                        values,
                    ):
                        raise _TransactionCanceled(len(operations), index)
                    staged[key] = item
                    continue

                update = operation["Update"]
                key_values = self.decode(update["Key"])
                key = self.key(key_values)
                current = staged.get(key)
                values = self.decode(
                    update.get("ExpressionAttributeValues", {})
                )
                if not self._condition_holds(
                    update.get("ConditionExpression"),
                    current,
                    values,
                ):
                    raise _TransactionCanceled(len(operations), index)
                staged[key] = self._apply_update(
                    update,
                    copy.deepcopy(current) if current is not None else key_values,
                    values,
                )

            self.rows = staged


class _CasTable:
    def __init__(self, client: _CasDynamoClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def put_item(
        self,
        *,
        Item: dict[str, Any],  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
        ExpressionAttributeNames: dict[str, str] | None = None,  # noqa: N803
        ExpressionAttributeValues: dict[str, Any] | None = None,  # noqa: N803
    ) -> None:
        del ExpressionAttributeNames
        with self._client._lock:
            key = self._client.key(Item)
            current = self._client.rows.get(key)
            if not self._client._condition_holds(
                ConditionExpression,
                current,
                ExpressionAttributeValues or {},
            ):
                raise _ConditionalCheckFailed
            self._client.rows[key] = copy.deepcopy(Item)

    def get_item(
        self,
        *,
        Key: dict[str, str],  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        with self._client._lock:
            row = self._client.rows.get((Key["PK"], Key["SK"]))
            return {"Item": copy.deepcopy(row)} if row is not None else {}

    def query(
        self,
        *,
        KeyConditionExpression: Any,  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        equals, begins_with = KeyConditionExpression._values
        partition = equals._values[1]
        prefix = begins_with._values[1]
        with self._client._lock:
            self._client.query_count += 1
            items = [
                copy.deepcopy(row)
                for (pk, sk), row in sorted(self._client.rows.items())
                if pk == partition and sk.startswith(prefix)
            ]
            hook = self._client.after_query_snapshot
            self._client.after_query_snapshot = None
            if hook is not None:
                hook(self._client)
        return {"Items": items}

    def scan(
        self,
        *,
        FilterExpression: Any,  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        entity_type = FilterExpression.get_expression()["values"][1]
        with self._client._lock:
            return {
                "Items": [
                    copy.deepcopy(row)
                    for row in self._client.rows.values()
                    if row.get("entity_type") == entity_type
                ]
            }


class _CasPersistence(DynamoPersistence):
    def __init__(self, client: _CasDynamoClient) -> None:
        super().__init__(table_name="cas-foundations-test")
        self._enabled = True
        self._table = _CasTable(client)


def _counter(client: _CasDynamoClient) -> int:
    row = client.rows.get(("CONFIG#VERSION", "TOTAL"))
    return int(row["version"]) if row is not None else 0


async def test_project_create_and_update_conflicts_are_revisioned() -> None:
    client = _CasDynamoClient()
    first = _CasPersistence(client)
    second = _CasPersistence(client)
    project = Project("project-a", "Original", budget_limit=100.0)

    assert await first.create_project(project) == 1
    with pytest.raises(ValueError, match="already exists"):
        await second.create_project(project)

    original = await first.get_project("project-a")
    assert original is not None
    assert original.revision == 1
    winner = replace(original, name="Winner")
    stale = replace(original, budget_limit=25.0)

    assert await first.save_project(winner) == 2
    with pytest.raises(PersistenceConflictError, match="concurrently"):
        await second.save_project(stale)

    stored = await first.get_project("project-a")
    assert stored is not None
    assert (stored.name, stored.budget_limit, stored.revision) == (
        "Winner",
        100.0,
        2,
    )
    assert _counter(client) == 2


async def test_tenant_project_update_is_atomic_with_config_version() -> None:
    client = _CasDynamoClient()
    first = _CasPersistence(client)
    second = _CasPersistence(client)
    project = Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Original",
        prompt_caching_enabled=False,
    )

    assert await first.create_project(project) == 1
    loaded = await first.get_project("shared", "tenant-a")
    assert loaded is not None
    assert loaded.revision == 1

    assert await first.save_project(
        replace(
            loaded,
            name="Updated",
            prompt_caching_enabled=True,
        ),
        expected_revision=1,
    ) == 2
    with pytest.raises(PersistenceConflictError):
        await second.save_project(
            replace(loaded, name="Stale"),
            expected_revision=1,
        )

    stored = await first.get_project("shared", "tenant-a")
    assert stored is not None
    assert stored.name == "Updated"
    assert stored.prompt_caching_enabled is True
    assert stored.revision == 2
    assert _counter(client) == 2


async def test_revisionless_project_row_migrates_once_with_cas() -> None:
    client = _CasDynamoClient()
    persistence = _CasPersistence(client)
    row = DynamoPersistence.serialize_project(
        Project("legacy", "Legacy", budget_limit=100.0)
    )
    row.pop("revision")
    client.rows[client.key(row)] = row
    client.rows[("CONFIG#VERSION", "TOTAL")] = {
        "PK": "CONFIG#VERSION",
        "SK": "TOTAL",
        "version": 9,
    }

    loaded = await persistence.get_project("legacy")
    assert loaded is not None
    assert loaded.revision == 0

    assert await persistence.save_project(
        replace(loaded, name="Migrated")
    ) == 1
    with pytest.raises(PersistenceConflictError):
        await persistence.save_project(
            replace(loaded, budget_limit=5.0)
        )

    stored = await persistence.get_project("legacy")
    assert stored is not None
    assert (stored.name, stored.budget_limit, stored.revision) == (
        "Migrated",
        100.0,
        1,
    )
    assert _counter(client) == 10


async def test_concurrent_user_config_replacements_preserve_the_conflict() -> None:
    client = _CasDynamoClient()
    first = _CasPersistence(client)
    second = _CasPersistence(client)
    baseline = {
        "allowed_models": ["model-a"],
        "budget_limit": 100.0,
        "alert_threshold": 80.0,
        "revision": 0,
    }
    assert await first.save_user_config("alice", baseline) == 1

    loaded = (await first.load_user_configs())["alice"]
    candidates = [
        {**loaded, "allowed_models": ["model-b"]},
        {**loaded, "budget_limit": 25.0},
    ]
    results = await asyncio.gather(
        first.save_user_config("alice", candidates[0]),
        second.save_user_config("alice", candidates[1]),
        return_exceptions=True,
    )

    winners = [
        index for index, result in enumerate(results) if result == 2
    ]
    conflicts = [
        result
        for result in results
        if isinstance(result, PersistenceConflictError)
    ]
    assert len(winners) == 1
    assert len(conflicts) == 1

    stored = (await first.load_user_configs())["alice"]
    expected = candidates[winners[0]]
    assert stored == {**expected, "revision": 2}
    assert _counter(client) == 2


@pytest.mark.parametrize("entity", ["project", "user_config"])
async def test_legacy_row_and_config_version_roll_back_together(
    entity: str,
) -> None:
    client = _CasDynamoClient()
    client.fail_at = 1
    persistence = _CasPersistence(client)

    with pytest.raises(RuntimeError):
        if entity == "project":
            await persistence.create_project(Project("rollback", "Rollback"))
        else:
            await persistence.save_user_config(
                "rollback",
                {"budget_limit": 10.0, "revision": 0},
            )

    assert client.rows == {}
    assert _counter(client) == 0


async def test_tenant_policy_hierarchy_create_and_update_conflicts() -> None:
    client = _CasDynamoClient()
    first = _CasPersistence(client)
    second = _CasPersistence(client)
    root = PolicyNode("org", "org", None, "Original")

    assert await first.save_tenant_policy_node(
        "tenant-a",
        root,
        expected_revision=0,
        create_only=True,
    ) == 1

    child = PolicyNode("project", "project", "org", "Project")
    with pytest.raises(PersistenceConflictError):
        await second.save_tenant_policy_node(
            "tenant-a",
            child,
            expected_revision=0,
            create_only=True,
        )
    assert (
        "TENANT#tenant-a",
        "POLICY_NODE#project",
    ) not in client.rows

    assert await first.save_tenant_policy_node(
        "tenant-a",
        child,
        expected_revision=1,
        create_only=True,
    ) == 2
    winner = replace(root, display_name="Winner")
    stale = replace(root, display_name="Stale")
    assert await first.save_tenant_policy_node(
        "tenant-a",
        winner,
        expected_revision=2,
        create_only=False,
    ) == 3
    with pytest.raises(PersistenceConflictError):
        await second.save_tenant_policy_node(
            "tenant-a",
            stale,
            expected_revision=2,
            create_only=False,
        )

    root_row = client.rows[
        ("TENANT#tenant-a", "POLICY_NODE#org")
    ]
    child_row = client.rows[
        ("TENANT#tenant-a", "POLICY_NODE#project")
    ]
    version_row = client.rows[
        ("TENANT#tenant-a", "POLICY_HIERARCHY#VERSION")
    ]
    assert DynamoPersistence.deserialize_policy_node(
        root_row
    ).display_name == "Winner"
    assert DynamoPersistence.deserialize_policy_node(
        child_row
    ).parent_id == "org"
    assert int(version_row["revision"]) == 3


async def test_tenant_policy_snapshot_retries_an_unstable_scan() -> None:
    client = _CasDynamoClient()
    persistence = _CasPersistence(client)
    root = PolicyNode("org", "org", None, "Org")
    assert await persistence.save_tenant_policy_node(
        "tenant-a",
        root,
        expected_revision=0,
        create_only=True,
    ) == 1

    def commit_child_during_query(db: _CasDynamoClient) -> None:
        child = PolicyNode("project", "project", "org", "Project")
        item = DynamoPersistence.serialize_tenant_policy_node(
            "tenant-a",
            child,
        )
        db.rows[db.key(item)] = item
        version = db.rows[
            ("TENANT#tenant-a", "POLICY_HIERARCHY#VERSION")
        ]
        version["revision"] = 2

    client.after_query_snapshot = commit_child_during_query
    nodes, revision = await persistence.load_tenant_policy_nodes_snapshot(
        "tenant-a"
    )

    assert revision == 2
    assert {node.node_id for node in nodes} == {"org", "project"}
    assert client.query_count == 2


async def test_membership_rejects_a_concurrent_non_member_project_edit() -> None:
    client = _CasDynamoClient()
    persistence = _CasPersistence(client)
    tenant_id = "tenant-a"
    project = Project(
        "project-a",
        "Project",
        tenant_id=tenant_id,
        budget_limit=100.0,
        revision=1,
    )
    user = ScimUser(
        id="user-a",
        user_name="alice@example.com",
        tenant_id=tenant_id,
        issuer="https://idp.example",
        subject="alice",
    )
    principal = Principal(
        principal_id="scim:user-a",
        tenant_id=tenant_id,
        subject=user.subject,
        issuer=user.issuer,
        roles=frozenset({TenantRole.TENANT_MEMBER}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
    )
    for row in (
        DynamoPersistence.serialize_project(project),
        DynamoPersistence._serialize_scim_user(user),
        DynamoPrincipalRepository.serialize(principal),
    ):
        client.rows[client.key(row)] = row

    project_key = ("TENANT#tenant-a", "PROJECT#project-a")

    def edit_budget_before_membership_cas(db: _CasDynamoClient) -> None:
        concurrent = copy.deepcopy(db.rows[project_key])
        concurrent["budget_limit"] = 25.0
        concurrent["revision"] = 2
        db.rows[project_key] = concurrent

    client.before_transaction = edit_budget_before_membership_cas
    with pytest.raises(CanonicalMembershipConflictError):
        await persistence.set_tenant_project_membership(
            tenant_id,
            "project-a",
            "user-a",
            granted=True,
        )

    stored_project = DynamoPersistence.deserialize_project(
        client.rows[project_key]
    )
    assert stored_project.budget_limit == 25.0
    assert stored_project.revision == 2
    assert stored_project.members == []
    assert client.rows[
        ("TENANT#tenant-a", "SCIM#USER#user-a")
    ]["authorization_version"] == 1
    assert ("TENANT#tenant-a", "SCIM#VERSION") not in client.rows


class _RecordingTracker:
    def __init__(self) -> None:
        self.projects: list[str] = []
        self.users: list[str] = []

    def register_project(self, project_id: str, **_kwargs: Any) -> None:
        self.projects.append(project_id)

    def register_user(self, user_id: str, **_kwargs: Any) -> None:
        self.users.append(user_id)


class _VersionRacePersistence:
    enabled = True

    def __init__(self) -> None:
        self._versions = iter((1, 2))

    async def get_config_version(self) -> int:
        return next(self._versions)

    async def load_projects_or_none(self) -> dict[str, Project]:
        return {"remote": Project("remote", "Remote", budget_limit=10.0)}

    async def load_user_configs_or_none(self) -> dict[str, dict]:
        return {
            "remote-user": {
                "budget_limit": 5.0,
                "revision": 1,
            }
        }


async def test_config_sync_discards_scans_when_version_changes() -> None:
    tracker = _RecordingTracker()
    projects = {"local": Project("local", "Local")}
    users = {"local-user": {"revision": 1}}
    sync = ConfigSyncService(
        projects,
        users,
        tracker,
        persistence=_VersionRacePersistence(),
    )
    sync._known_version = 0

    assert await sync.refresh_if_stale() is False
    assert set(projects) == {"local"}
    assert set(users) == {"local-user"}
    assert sync._known_version == 0
    assert sync._last_version_check == float("-inf")
    assert tracker.projects == []
    assert tracker.users == []


class _GenerationRacePersistence:
    enabled = True

    def __init__(
        self,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._started = started
        self._release = release

    async def get_config_version(self) -> int:
        return 1

    async def load_projects_or_none(self) -> dict[str, Project]:
        self._started.set()
        await self._release.wait()
        return {"remote": Project("remote", "Stale scan")}

    async def load_user_configs_or_none(self) -> dict[str, dict]:
        await self._release.wait()
        return {"remote-user": {"revision": 1}}


async def test_config_sync_discards_scans_when_local_generation_changes() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    tracker = _RecordingTracker()
    projects = {"local": Project("local", "Local")}
    users = {"local-user": {"revision": 1}}
    sync = ConfigSyncService(
        projects,
        users,
        tracker,
        persistence=_GenerationRacePersistence(started, release),
    )
    sync._known_version = 0

    refresh = asyncio.create_task(sync.refresh_if_stale())
    await started.wait()
    projects["just-committed"] = Project(
        "just-committed",
        "Local commit",
    )
    sync.invalidate_local_config()
    release.set()

    assert await refresh is False
    assert set(projects) == {"local", "just-committed"}
    assert set(users) == {"local-user"}
    assert sync._known_version == 0
    assert sync._last_version_check == float("-inf")
    assert tracker.projects == []
    assert tracker.users == []
