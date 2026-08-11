"""Focused CAS and isolation tests for datasource repositories."""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src.gateway.persistence import (
    DynamoPersistence,
    PersistenceConflictError,
    PersistenceQuotaExceededError,
)
from src.gateway.query.models import AthenaDatasource
from src.gateway.query.repository import (
    DatasourceConflictError,
    DatasourceCursorError,
    DatasourceQuotaExceededError,
    DatasourceStoreUnavailable,
    DynamoDatasourceRepository,
    InMemoryDatasourceRepository,
)


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"


def _datasource(
    *,
    datasource_id: str = "warehouse",
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
    name: str = "Analytics warehouse",
) -> AthenaDatasource:
    return AthenaDatasource(
        datasource_id=datasource_id,
        tenant_id=tenant_id,
        project_id=project_id,
        name=name,
        role_arn=ROLE_ARN,
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
    )


async def test_in_memory_create_update_and_delete_use_cas() -> None:
    repository = InMemoryDatasourceRepository()

    created = await repository.save(
        _datasource(),
        expected_revision=0,
    )
    assert created.revision == 1

    with pytest.raises(DatasourceConflictError):
        await repository.save(
            replace(created, name="Stale create"),
            expected_revision=0,
        )

    updated = await repository.save(
        replace(created, name="Updated warehouse"),
        expected_revision=1,
    )
    assert updated.revision == 2
    assert updated.name == "Updated warehouse"
    assert updated.created_at == created.created_at

    with pytest.raises(DatasourceConflictError):
        await repository.delete(
            "tenant-a",
            "project-a",
            "warehouse",
            expected_revision=1,
        )
    assert (
        await repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
        == updated
    )

    await repository.delete(
        "tenant-a",
        "project-a",
        "warehouse",
        expected_revision=2,
    )
    assert (
        await repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
        is None
    )
    with pytest.raises(DatasourceConflictError):
        await repository.delete(
            "tenant-a",
            "project-a",
            "warehouse",
            expected_revision=2,
        )


async def test_concurrent_updates_have_exactly_one_cas_winner() -> None:
    repository = InMemoryDatasourceRepository()
    created = await repository.save(
        _datasource(),
        expected_revision=0,
    )

    outcomes = await asyncio.gather(
        repository.save(
            replace(created, name="Candidate A"),
            expected_revision=1,
        ),
        repository.save(
            replace(created, name="Candidate B"),
            expected_revision=1,
        ),
        return_exceptions=True,
    )

    winners = [
        result
        for result in outcomes
        if isinstance(result, AthenaDatasource)
    ]
    conflicts = [
        result
        for result in outcomes
        if isinstance(result, DatasourceConflictError)
    ]
    assert len(winners) == 1
    assert winners[0].revision == 2
    assert len(conflicts) == 1
    assert (
        await repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
        == winners[0]
    )


async def test_datasource_quota_is_tenant_scoped_and_atomic() -> None:
    repository = InMemoryDatasourceRepository(
        max_datasources_per_tenant=1
    )
    await repository.save(_datasource(), expected_revision=0)

    with pytest.raises(DatasourceQuotaExceededError):
        await repository.save(
            _datasource(datasource_id="second"),
            expected_revision=0,
        )

    await repository.save(
        _datasource(
            datasource_id="other-tenant",
            tenant_id="tenant-b",
        ),
        expected_revision=0,
    )


async def test_in_memory_listing_is_tenant_scoped_filtered_and_sorted() -> None:
    repository = InMemoryDatasourceRepository()
    for datasource in (
        _datasource(
            datasource_id="zeta",
            project_id="project-b",
        ),
        _datasource(
            datasource_id="beta",
            project_id="project-a",
        ),
        _datasource(
            datasource_id="alpha",
            project_id="project-a",
        ),
        _datasource(
            datasource_id="hidden",
            tenant_id="tenant-b",
            project_id="project-a",
        ),
    ):
        await repository.save(datasource, expected_revision=0)

    tenant_page = await repository.list("tenant-a")
    project_page = await repository.list(
        "tenant-a",
        project_id="project-a",
    )

    assert [
        (item.project_id, item.datasource_id)
        for item in tenant_page.items
    ] == [
        ("project-a", "alpha"),
        ("project-a", "beta"),
        ("project-b", "zeta"),
    ]
    assert [
        item.datasource_id for item in project_page.items
    ] == ["alpha", "beta"]
    assert (await repository.list("missing-tenant")).items == ()


async def test_in_memory_listing_uses_bounded_opaque_cursors() -> None:
    repository = InMemoryDatasourceRepository()
    for datasource_id in ("alpha", "beta", "gamma"):
        await repository.save(
            _datasource(datasource_id=datasource_id),
            expected_revision=0,
        )

    first = await repository.list("tenant-a", limit=2)
    second = await repository.list(
        "tenant-a",
        limit=2,
        cursor=first.next_cursor,
    )

    assert [item.datasource_id for item in first.items] == [
        "alpha",
        "beta",
    ]
    assert first.next_cursor is not None
    assert "DATASOURCE" not in first.next_cursor
    assert [item.datasource_id for item in second.items] == ["gamma"]
    assert second.next_cursor is None
    with pytest.raises(DatasourceCursorError):
        await repository.list("tenant-a", cursor="not-a-cursor")


class _Persistence:
    def __init__(self) -> None:
        self.values: list[dict[str, Any]] = []
        self.get_value: dict[str, Any] | None = None
        self.save_calls: list[dict[str, Any]] = []
        self.get_calls: list[tuple[str, str, str]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.next_key: str | None = None
        self.delete_calls: list[dict[str, Any]] = []
        self.save_error: Exception | None = None
        self.get_error: Exception | None = None
        self.list_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def save_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        document: dict[str, Any],
        *,
        expected_revision: int,
        max_datasources: int = 500,
    ) -> int:
        self.save_calls.append(
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "datasource_id": datasource_id,
                "document": dict(document),
                "expected_revision": expected_revision,
                "max_datasources": max_datasources,
            }
        )
        if self.save_error is not None:
            raise self.save_error
        return expected_revision + 1

    async def get_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> dict[str, Any] | None:
        self.get_calls.append((tenant_id, project_id, datasource_id))
        if self.get_error is not None:
            raise self.get_error
        return self.get_value

    async def list_tenant_datasources(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
        exclusive_start_key: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self.list_calls.append(
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "limit": limit,
                "exclusive_start_key": exclusive_start_key,
            }
        )
        if self.list_error is not None:
            raise self.list_error
        return list(self.values), self.next_key

    async def delete_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        *,
        expected_revision: int,
        max_datasources: int = 500,
    ) -> None:
        self.delete_calls.append(
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "datasource_id": datasource_id,
                "expected_revision": expected_revision,
                "max_datasources": max_datasources,
            }
        )
        if self.delete_error is not None:
            raise self.delete_error


def _stored(
    *,
    datasource_id: str = "warehouse",
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
    revision: int = 1,
) -> dict[str, Any]:
    value = _datasource(
        datasource_id=datasource_id,
        tenant_id=tenant_id,
        project_id=project_id,
    ).to_dict()
    value["revision"] = revision
    return value


def _persistence_document() -> dict[str, Any]:
    value = _datasource().to_dict()
    for key in (
        "tenant_id",
        "project_id",
        "datasource_id",
        "revision",
    ):
        value.pop(key)
    return value


class _DatasourceTransactionCanceled(RuntimeError):
    def __init__(self, item_count: int, failed_index: int) -> None:
        reasons = [{"Code": "None"} for _ in range(item_count)]
        reasons[failed_index] = {"Code": "ConditionalCheckFailed"}
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        }
        super().__init__("datasource transaction condition failed")


class _DatasourceConditionalCheckFailed(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException"},
        }
        super().__init__("datasource condition failed")


class _DatasourceTransactionalClient:
    """All-or-nothing interpreter for datasource quota transactions."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[dict[str, Any]] = []
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

    def transact_write_items(self, **request: Any) -> None:
        with self._lock:
            self.transactions.append(copy.deepcopy(request))
            operations = request["TransactItems"]
            staged = copy.deepcopy(self.rows)

            for index, operation in enumerate(operations):
                if "Put" in operation:
                    item = self.decode(operation["Put"]["Item"])
                    key = self.key(item)
                    if key in staged:
                        raise _DatasourceTransactionCanceled(
                            len(operations),
                            index,
                        )
                    staged[key] = item
                    continue

                if "Delete" in operation:
                    delete = operation["Delete"]
                    key = self.key(self.decode(delete["Key"]))
                    current = staged.get(key)
                    values = self.decode(
                        delete["ExpressionAttributeValues"]
                    )
                    if (
                        current is None
                        or current.get("entity_type")
                        != values[":entity_type"]
                        or current.get("revision") != values[":expected"]
                    ):
                        raise _DatasourceTransactionCanceled(
                            len(operations),
                            index,
                        )
                    del staged[key]
                    continue

                update = operation["Update"]
                key = self.key(self.decode(update["Key"]))
                current = staged.get(key)
                values = self.decode(
                    update["ExpressionAttributeValues"]
                )
                count = (
                    current.get("datasource_count")
                    if current is not None
                    else None
                )
                if (
                    current is None
                    or current.get("entity_type")
                    != values[":entity_type"]
                    or current.get("tenant_id") != values[":tenant_id"]
                    or (
                        ":limit" in values
                        and not count < values[":limit"]
                    )
                    or (
                        ":zero" in values
                        and not count > values[":zero"]
                    )
                ):
                    raise _DatasourceTransactionCanceled(
                        len(operations),
                        index,
                    )
                delta = values.get(":one", values.get(":minus_one"))
                changed = copy.deepcopy(current)
                changed["datasource_count"] = count + delta
                staged[key] = changed

            self.rows = staged


class _DatasourceTable:
    def __init__(self, client: _DatasourceTransactionalClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def get_item(
        self,
        *,
        Key: dict[str, str],  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        with self._client._lock:
            item = self._client.rows.get((Key["PK"], Key["SK"]))
            return {"Item": copy.deepcopy(item)} if item is not None else {}

    def query(
        self,
        *,
        KeyConditionExpression: Any,  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
        Select: str | None = None,  # noqa: N803
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        assert Select == "COUNT"
        equals, begins_with = KeyConditionExpression._values
        partition = equals._values[1]
        prefix = begins_with._values[1]
        with self._client._lock:
            count = sum(
                1
                for pk, sk in self._client.rows
                if pk == partition and sk.startswith(prefix)
            )
        return {"Count": count}

    def put_item(
        self,
        *,
        Item: dict[str, Any],  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
    ) -> None:
        assert ConditionExpression == (
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        )
        key = self._client.key(Item)
        with self._client._lock:
            if key in self._client.rows:
                raise _DatasourceConditionalCheckFailed
            self._client.rows[key] = copy.deepcopy(Item)


def _datasource_store() -> tuple[
    DynamoPersistence,
    _DatasourceTransactionalClient,
]:
    store = DynamoPersistence(table_name="state")
    store._enabled = True
    client = _DatasourceTransactionalClient()
    store._table = _DatasourceTable(client)
    return store, client


def _quota_count(client: _DatasourceTransactionalClient) -> int:
    quota = client.rows[
        ("TENANT#tenant-a", "QUOTA#ATHENA_DATASOURCES")
    ]
    return int(quota["datasource_count"])


def test_low_level_serializer_enforces_credential_free_schema() -> None:
    document = _persistence_document()

    item = DynamoPersistence.serialize_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        document,
        revision=1,
    )

    assert item["PK"] == "TENANT#tenant-a"
    assert item["SK"] == "DATASOURCE#project-a#warehouse"
    assert "secret" not in item["document"].casefold()

    with pytest.raises(ValueError, match="credential-free schema"):
        DynamoPersistence.serialize_tenant_datasource(
            "tenant-a",
            "project-a",
            "warehouse",
            {**document, "secret_access_key": "must-not-persist"},
            revision=1,
        )


@pytest.mark.parametrize(
    ("project_id", "datasource_id"),
    [
        ("project#a", "warehouse"),
        ("project-a", "warehouse#other"),
        (" project-a", "warehouse"),
    ],
)
def test_low_level_serializer_rejects_ambiguous_sort_key_identity(
    project_id: str,
    datasource_id: str,
) -> None:
    with pytest.raises(ValueError, match="delimiter-safe"):
        DynamoPersistence.serialize_tenant_datasource(
            "tenant-a",
            project_id,
            datasource_id,
            _persistence_document(),
            revision=1,
        )


async def test_low_level_dynamo_create_enforces_quota_atomically() -> None:
    store, client = _datasource_store()

    revision = await store.save_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        _persistence_document(),
        expected_revision=0,
        max_datasources=1,
    )

    assert revision == 1
    assert _quota_count(client) == 1
    assert (
        "TENANT#tenant-a",
        "DATASOURCE#project-a#warehouse",
    ) in client.rows

    with pytest.raises(
        PersistenceQuotaExceededError,
        match="quota exceeded",
    ):
        await store.save_tenant_datasource(
            "tenant-a",
            "project-a",
            "second",
            _persistence_document(),
            expected_revision=0,
            max_datasources=1,
        )

    assert _quota_count(client) == 1
    assert (
        "TENANT#tenant-a",
        "DATASOURCE#project-a#second",
    ) not in client.rows


async def test_low_level_duplicate_create_does_not_increment_quota() -> None:
    store, client = _datasource_store()
    await store.save_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        _persistence_document(),
        expected_revision=0,
        max_datasources=2,
    )

    with pytest.raises(
        PersistenceConflictError,
        match="changed concurrently",
    ):
        await store.save_tenant_datasource(
            "tenant-a",
            "project-a",
            "warehouse",
            _persistence_document(),
            expected_revision=0,
            max_datasources=2,
        )

    assert _quota_count(client) == 1


async def test_low_level_delete_and_counter_change_are_atomic() -> None:
    store, client = _datasource_store()
    await store.save_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        _persistence_document(),
        expected_revision=0,
    )
    datasource_key = (
        "TENANT#tenant-a",
        "DATASOURCE#project-a#warehouse",
    )

    with pytest.raises(
        PersistenceConflictError,
        match="changed concurrently",
    ):
        await store.delete_tenant_datasource(
            "tenant-a",
            "project-a",
            "warehouse",
            expected_revision=2,
        )

    assert datasource_key in client.rows
    assert _quota_count(client) == 1

    await store.delete_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        expected_revision=1,
    )

    assert datasource_key not in client.rows
    assert _quota_count(client) == 0


async def test_low_level_legacy_rows_seed_the_quota_counter() -> None:
    store, client = _datasource_store()
    legacy = DynamoPersistence.serialize_tenant_datasource(
        "tenant-a",
        "project-a",
        "legacy",
        _persistence_document(),
        revision=1,
    )
    client.rows[client.key(legacy)] = legacy

    await store.save_tenant_datasource(
        "tenant-a",
        "project-a",
        "warehouse",
        _persistence_document(),
        expected_revision=0,
        max_datasources=2,
    )

    assert _quota_count(client) == 2


async def test_low_level_concurrent_creates_have_one_quota_winner() -> None:
    store, client = _datasource_store()

    outcomes = await asyncio.gather(
        store.save_tenant_datasource(
            "tenant-a",
            "project-a",
            "first",
            _persistence_document(),
            expected_revision=0,
            max_datasources=1,
        ),
        store.save_tenant_datasource(
            "tenant-a",
            "project-a",
            "second",
            _persistence_document(),
            expected_revision=0,
            max_datasources=1,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, int) for outcome in outcomes) == 1
    assert (
        sum(
            isinstance(outcome, PersistenceQuotaExceededError)
            for outcome in outcomes
        )
        == 1
    )
    assert _quota_count(client) == 1
    assert (
        sum(
            sk.startswith("DATASOURCE#")
            for pk, sk in client.rows
            if pk == "TENANT#tenant-a"
        )
        == 1
    )


async def test_dynamo_repository_forwards_cas_and_system_metadata() -> None:
    persistence = _Persistence()
    repository = DynamoDatasourceRepository(persistence)
    datasource = _datasource()

    saved = await repository.save(
        datasource,
        expected_revision=0,
    )

    assert saved.revision == 1
    assert len(persistence.save_calls) == 1
    call = persistence.save_calls[0]
    assert call["tenant_id"] == "tenant-a"
    assert call["project_id"] == "project-a"
    assert call["datasource_id"] == "warehouse"
    assert call["expected_revision"] == 0
    assert {
        "tenant_id",
        "project_id",
        "datasource_id",
        "revision",
    }.isdisjoint(call["document"])
    assert call["document"]["created_at"] == saved.created_at
    assert call["document"]["updated_at"] == saved.updated_at
    assert "access_key_id" not in call["document"]
    assert "secret_access_key" not in call["document"]


async def test_dynamo_repository_deserializes_and_sorts_canonical_state() -> None:
    persistence = _Persistence()
    repository = DynamoDatasourceRepository(persistence)
    persistence.get_value = _stored(revision=3)
    persistence.values = [
        _stored(
            datasource_id="zeta",
            project_id="project-b",
        ),
        _stored(datasource_id="beta"),
        _stored(datasource_id="alpha"),
    ]

    loaded = await repository.get(
        "tenant-a",
        "project-a",
        "warehouse",
    )
    listed = await repository.list(
        "tenant-a",
    )

    assert loaded is not None
    assert loaded.revision == 3
    assert loaded.tenant_id == "tenant-a"
    assert persistence.get_calls == [
        ("tenant-a", "project-a", "warehouse")
    ]
    assert persistence.list_calls == [
        {
            "tenant_id": "tenant-a",
            "project_id": None,
            "limit": 50,
            "exclusive_start_key": None,
        }
    ]
    assert [
        (item.project_id, item.datasource_id)
        for item in listed.items
    ] == [
        ("project-a", "alpha"),
        ("project-a", "beta"),
        ("project-b", "zeta"),
    ]


@pytest.mark.parametrize(
    "operation",
    ["get", "list"],
)
async def test_dynamo_repository_rejects_mismatched_owner(
    operation: str,
) -> None:
    persistence = _Persistence()
    repository = DynamoDatasourceRepository(persistence)
    mismatched = _stored(tenant_id="tenant-b")
    if operation == "get":
        persistence.get_value = mismatched
        action = repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
    else:
        persistence.values = [mismatched]
        action = repository.list("tenant-a")

    with pytest.raises(
        DatasourceStoreUnavailable,
        match="authority",
    ):
        await action


@pytest.mark.parametrize(
    ("operation", "revision"),
    [
        ("save", -1),
        ("save", True),
        ("delete", 0),
        ("delete", False),
    ],
)
async def test_repositories_reject_invalid_expected_revision(
    operation: str,
    revision: object,
) -> None:
    for repository in (
        InMemoryDatasourceRepository(),
        DynamoDatasourceRepository(_Persistence()),
    ):
        if operation == "save":
            action = repository.save(
                _datasource(),
                expected_revision=revision,
            )
        else:
            action = repository.delete(
                "tenant-a",
                "project-a",
                "warehouse",
                expected_revision=revision,
            )
        with pytest.raises(ValueError, match="expected_revision"):
            await action


@pytest.mark.parametrize("operation", ["save", "delete"])
async def test_dynamo_repository_translates_persistence_conflicts(
    operation: str,
) -> None:
    persistence = _Persistence()
    repository = DynamoDatasourceRepository(persistence)
    conflict = PersistenceConflictError("datasource changed concurrently")

    if operation == "save":
        persistence.save_error = conflict
        action = repository.save(
            _datasource(),
            expected_revision=1,
        )
    else:
        persistence.delete_error = conflict
        action = repository.delete(
            "tenant-a",
            "project-a",
            "warehouse",
            expected_revision=1,
        )

    with pytest.raises(
        DatasourceConflictError,
        match="changed concurrently",
    ):
        await action


async def test_dynamo_repository_translates_datasource_quota() -> None:
    persistence = _Persistence()
    persistence.save_error = PersistenceQuotaExceededError(
        "tenant datasource quota exceeded"
    )
    repository = DynamoDatasourceRepository(persistence)

    with pytest.raises(DatasourceQuotaExceededError):
        await repository.save(
            _datasource(),
            expected_revision=0,
        )


@pytest.mark.parametrize("operation", ["save", "get", "list", "delete"])
async def test_dynamo_repository_fails_closed_when_store_is_unavailable(
    operation: str,
) -> None:
    persistence = _Persistence()
    repository = DynamoDatasourceRepository(persistence)
    failure = RuntimeError("DynamoDB unavailable")
    if operation == "save":
        persistence.save_error = failure
        action = repository.save(
            _datasource(),
            expected_revision=0,
        )
    elif operation == "get":
        persistence.get_error = failure
        action = repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
    elif operation == "list":
        persistence.list_error = failure
        action = repository.list("tenant-a")
    else:
        persistence.delete_error = failure
        action = repository.delete(
            "tenant-a",
            "project-a",
            "warehouse",
            expected_revision=1,
        )

    with pytest.raises(DatasourceStoreUnavailable):
        await action


async def test_dynamo_repository_rejects_malformed_canonical_state() -> None:
    persistence = _Persistence()
    persistence.get_value = _stored()
    persistence.get_value.pop("created_at")
    repository = DynamoDatasourceRepository(persistence)

    with pytest.raises(
        DatasourceStoreUnavailable,
        match="malformed state",
    ):
        await repository.get(
            "tenant-a",
            "project-a",
            "warehouse",
        )
