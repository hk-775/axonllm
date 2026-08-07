"""Transactional durability tests for API-key persistence."""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.models import APIKey
from src.gateway.persistence import DynamoPersistence


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
    """Small all-or-nothing interpreter for the two transaction shapes."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.transactions: list[dict] = []
        self.fail_at: int | None = None
        self._lock = threading.Lock()
        self._deserializer = TypeDeserializer()

    def _decode(self, values: dict) -> dict:
        return {
            name: self._deserializer.deserialize(value)
            for name, value in values.items()
        }

    @staticmethod
    def _key(item: dict) -> tuple[str, str]:
        return item["PK"], item["SK"]

    def transact_write_items(self, **request) -> None:
        with self._lock:
            self.transactions.append(copy.deepcopy(request))
            items = request["TransactItems"]
            staged = copy.deepcopy(self.rows)

            for index, operation in enumerate(items):
                if self.fail_at == index:
                    raise RuntimeError(f"injected item {index} failure")

                if "Put" in operation:
                    item = self._decode(operation["Put"]["Item"])
                    storage_key = self._key(item)
                    if storage_key in staged:
                        raise _TransactionCanceled(len(items), index)
                    staged[storage_key] = item
                    continue

                update = operation["Update"]
                storage_key = self._key(self._decode(update["Key"]))
                values = self._decode(update["ExpressionAttributeValues"])
                if update["UpdateExpression"].startswith("SET revoked"):
                    current = staged.get(storage_key)
                    if (
                        current is None
                        or current.get("key_hash") != values[":key_hash"]
                        or current.get("revoked", False)
                    ):
                        raise _TransactionCanceled(len(items), index)
                    changed = copy.deepcopy(current)
                    changed["revoked"] = values[":true"]
                    changed["revoked_at"] = values[":revoked_at"]
                    staged[storage_key] = changed
                else:
                    current = copy.deepcopy(
                        staged.get(
                            storage_key,
                            {"PK": storage_key[0], "SK": storage_key[1]},
                        )
                    )
                    current["epoch"] = current.get("epoch", 0) + values[":one"]
                    staged[storage_key] = current

            self.rows = staged


class _Table:
    def __init__(self, client: _TransactionalClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True
        item = self._client.rows.get((Key["PK"], Key["SK"]))
        return {"Item": copy.deepcopy(item)} if item is not None else {}


class _Persistence(DynamoPersistence):
    def __init__(self, client: _TransactionalClient) -> None:
        super().__init__(table_name="api-key-test")
        self._enabled = True
        self._table = _Table(client)


def _key(
    *,
    tenant_id: str | None = "tenant-a",
    key_id: str = "axk_test",
    key_hash: str = "a" * 64,
) -> APIKey:
    return APIKey(
        key_id=key_id,
        key_hash=key_hash,
        project_id="project-a",
        name="Production key",
        scopes=["chat:invoke"],
        created_by="principal-a",
        tenant_id=tenant_id,
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _run_concurrently(*coroutines):
    async def _gather():
        return await asyncio.gather(*coroutines, return_exceptions=True)

    return asyncio.run(_gather())


class TestTenantQualifiedSerialization:
    def test_tenant_id_round_trips_and_qualifies_primary_namespace(self):
        item = DynamoPersistence.serialize_api_key(_key())
        restored = DynamoPersistence.deserialize_api_key(item)

        assert item["PK"] == "TENANT#tenant-a#APIKEY#axk_test"
        assert item["SK"] == "METADATA"
        assert item["tenant_id"] == "tenant-a"
        assert restored.tenant_id == "tenant-a"

    def test_legacy_key_shape_remains_readable(self):
        item = DynamoPersistence.serialize_api_key(_key(tenant_id=None))
        restored = DynamoPersistence.deserialize_api_key(item)

        assert item["PK"] == "APIKEY#axk_test"
        assert item["SK"] == "APIKEY"
        assert "tenant_id" not in item
        assert restored.tenant_id is None


class TestFailClosedLookup:
    def test_hash_lookup_rejects_mismatched_tenant_metadata(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        key = _key()
        asyncio.run(persistence.save_api_key(key))
        client.rows[
            ("TENANT#tenant-a#APIKEY#axk_test", "METADATA")
        ]["tenant_id"] = "tenant-b"

        assert asyncio.run(persistence.get_api_key_by_hash(key.key_hash)) is None


class TestAtomicIssuance:
    def test_one_transaction_creates_primary_hash_and_project_rows(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)

        asyncio.run(persistence.save_api_key(_key()))

        assert set(client.rows) == {
            ("TENANT#tenant-a#APIKEY#axk_test", "METADATA"),
            ("APIKEY_HASH#" + "a" * 64, "LOOKUP"),
            ("TENANT#tenant-a#PROJECT#project-a", "APIKEY#axk_test"),
        }
        transaction = client.transactions[0]["TransactItems"]
        assert len(transaction) == 3
        assert all("Put" in operation for operation in transaction)
        assert all(
            operation["Put"]["ConditionExpression"]
            == "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            for operation in transaction
        )

    @pytest.mark.parametrize("failed_index", [0, 1, 2])
    def test_any_item_failure_leaves_no_partial_credential(self, failed_index):
        client = _TransactionalClient()
        client.fail_at = failed_index
        persistence = _Persistence(client)

        with pytest.raises(RuntimeError, match="API key transaction failed"):
            asyncio.run(persistence.save_api_key(_key()))

        assert client.rows == {}

    def test_concurrent_duplicate_creation_has_one_winner(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)

        results = _run_concurrently(
            persistence.save_api_key(_key()),
            persistence.save_api_key(_key()),
        )

        assert sum(result is None for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert "already exists" in str(failures[0])
        assert len(client.rows) == 3


class TestAtomicRevocation:
    def test_key_and_tenant_epoch_change_in_one_transaction(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        key = _key()
        asyncio.run(persistence.save_api_key(key))

        revoked = replace(
            key,
            revoked=True,
            revoked_at=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        )
        assert asyncio.run(persistence.revoke_api_key(revoked)) is True

        primary = client.rows[
            ("TENANT#tenant-a#APIKEY#axk_test", "METADATA")
        ]
        epoch = client.rows[("TENANT#tenant-a", "AUTHZ#EPOCH")]
        assert primary["revoked"] is True
        assert primary["revoked_at"] == revoked.revoked_at.isoformat()
        assert epoch["epoch"] == 1
        transaction = client.transactions[-1]["TransactItems"]
        assert len(transaction) == 2
        assert all("Update" in operation for operation in transaction)

    @pytest.mark.parametrize("failed_index", [0, 1])
    def test_any_item_failure_preserves_key_and_epoch(self, failed_index):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        key = _key()
        asyncio.run(persistence.save_api_key(key))
        before = copy.deepcopy(client.rows)
        client.fail_at = failed_index

        revoked = replace(
            key,
            revoked=True,
            revoked_at=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        )
        with pytest.raises(
            RuntimeError,
            match="API key revocation transaction failed",
        ):
            asyncio.run(persistence.revoke_api_key(revoked))

        assert client.rows == before
        assert ("TENANT#tenant-a", "AUTHZ#EPOCH") not in client.rows

    def test_concurrent_revocation_has_one_winner_and_one_epoch_increment(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        key = _key()
        asyncio.run(persistence.save_api_key(key))
        revoked = replace(
            key,
            revoked=True,
            revoked_at=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        )

        results = _run_concurrently(
            persistence.revoke_api_key(revoked),
            persistence.revoke_api_key(revoked),
        )

        assert sorted(results) == [False, True]
        assert client.rows[("TENANT#tenant-a", "AUTHZ#EPOCH")]["epoch"] == 1

    def test_hash_lookup_cannot_reload_active_state_after_success(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        key = _key()
        asyncio.run(persistence.save_api_key(key))
        revoked = replace(
            key,
            revoked=True,
            revoked_at=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        )

        assert asyncio.run(persistence.revoke_api_key(revoked)) is True
        loaded = asyncio.run(persistence.get_api_key_by_hash(key.key_hash))

        assert loaded is not None
        assert loaded.revoked is True
        assert asyncio.run(persistence.get_revocation_epoch("tenant-a")) == 1

    def test_tenant_epoch_evicts_another_services_cached_key(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        issuer = APIKeyService(persistence)
        other_replica = APIKeyService(persistence)
        key, raw = asyncio.run(
            issuer.issue_key(
                "project-a",
                "Production key",
                ["chat:invoke"],
                "principal-a",
                tenant_id="tenant-a",
            )
        )

        assert asyncio.run(other_replica.validate_key(raw)) is not None
        assert asyncio.run(issuer.revoke_key(key.key_id, "tenant-a")) is True
        other_replica._tenant_revocation_checked_at["tenant-a"] = 0.0

        assert asyncio.run(other_replica.validate_key(raw)) is None

    def test_first_epoch_baseline_rechecks_key_cached_during_issuance(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        issuer = APIKeyService(persistence)
        revoker = APIKeyService(persistence)
        key, raw = asyncio.run(
            issuer.issue_key(
                "project-a",
                "Production key",
                ["chat:invoke"],
                "principal-a",
                tenant_id="tenant-a",
            )
        )

        # The issuer has the active object cached but has never read the tenant
        # epoch. Revocation by another replica must not become its baseline and
        # leave that stale object usable for the full cache TTL.
        assert asyncio.run(revoker.revoke_key(key.key_id, "tenant-a")) is True
        assert issuer._tenant_revocation_epochs == {}

        assert asyncio.run(issuer.validate_key(raw)) is None

    def test_changed_epoch_rechecks_key_loaded_before_concurrent_revocation(self):
        client = _TransactionalClient()
        persistence = _Persistence(client)
        issuer = APIKeyService(persistence)
        validator = APIKeyService(persistence)
        revoker = APIKeyService(persistence)
        key, raw = asyncio.run(
            issuer.issue_key(
                "project-a",
                "Production key",
                ["chat:invoke"],
                "principal-a",
                tenant_id="tenant-a",
            )
        )

        # Establish an epoch-0 baseline without putting the key in validator's
        # cache. The regression requires a known baseline, not first-use setup.
        asyncio.run(validator._check_revocations("tenant-a"))
        assert validator._tenant_revocation_epochs["tenant-a"] == 0
        validator._tenant_revocation_checked_at["tenant-a"] = 0.0

        original_epoch_read = persistence.get_revocation_epoch
        original_key_read = persistence.get_api_key_by_hash
        key_reads = 0
        revoked_between_reads = False

        async def _counted_key_read(key_hash):
            nonlocal key_reads
            key_reads += 1
            return await original_key_read(key_hash)

        async def _revoke_then_read_epoch(tenant_id=None):
            nonlocal revoked_between_reads
            if not revoked_between_reads:
                revoked_between_reads = True
                assert await revoker.revoke_key(key.key_id, "tenant-a") is True
            return await original_epoch_read(tenant_id)

        persistence.get_api_key_by_hash = _counted_key_read
        persistence.get_revocation_epoch = _revoke_then_read_epoch

        assert asyncio.run(validator.validate_key(raw)) is None
        assert revoked_between_reads is True
        assert key_reads == 2
        assert key.key_hash not in validator._cache
