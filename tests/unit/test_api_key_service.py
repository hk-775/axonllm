"""Tests for API key management service."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.auth.api_key_service import APIKeyService, PREFIX
from src.gateway.models import APIKey


class FakePersistence:
    """In-memory persistence for testing."""

    def __init__(self):
        self._keys: dict[str, APIKey] = {}
        self._hash_index: dict[str, str] = {}
        self._enabled = True

    @property
    def enabled(self):
        return self._enabled

    async def save_api_key(self, key: APIKey) -> None:
        self._keys[key.key_id] = key
        self._hash_index[key.key_hash] = key.key_id

    async def get_api_key_by_hash(self, key_hash: str) -> APIKey | None:
        key_id = self._hash_index.get(key_hash)
        if key_id:
            return self._keys.get(key_id)
        return None

    async def get_api_key(self, key_id: str) -> APIKey | None:
        return self._keys.get(key_id)

    async def list_api_keys_for_project(self, project_id: str) -> list[APIKey]:
        return [k for k in self._keys.values() if k.project_id == project_id]

    async def update_api_key(self, key: APIKey) -> None:
        self._keys[key.key_id] = key


@pytest.fixture
def service():
    return APIKeyService(persistence=FakePersistence())


class TestIssueKey:
    def test_returns_key_and_raw(self, service):
        key, raw = asyncio.run(
            service.issue_key("proj-1", "Test Key", ["chat:invoke"], "admin")
        )
        assert key.project_id == "proj-1"
        assert key.name == "Test Key"
        assert key.scopes == ["chat:invoke"]
        assert raw.startswith(PREFIX)
        assert len(raw) == len(PREFIX) + 64  # 32 bytes hex = 64 chars

    def test_key_id_has_prefix(self, service):
        key, _ = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        assert key.key_id.startswith("axk_")

    def test_hash_stored_not_raw(self, service):
        key, raw = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        assert key.key_hash == APIKeyService.hash_key(raw)
        assert raw not in key.key_hash


class TestValidateKey:
    def test_valid_key_returns_record(self, service):
        key, raw = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        result = asyncio.run(service.validate_key(raw))
        assert result is not None
        assert result.key_id == key.key_id

    def test_wrong_key_returns_none(self, service):
        asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        result = asyncio.run(
            service.validate_key("axon_" + "a" * 64)
        )
        assert result is None

    def test_non_prefixed_key_returns_none(self, service):
        result = asyncio.run(
            service.validate_key("not-a-valid-key")
        )
        assert result is None

    def test_revoked_key_returns_none(self, service):
        key, raw = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        asyncio.run(service.revoke_key(key.key_id))
        result = asyncio.run(service.validate_key(raw))
        assert result is None

    def test_expired_key_returns_none(self, service):
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        key, raw = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin", expires_at=expires_at)
        )
        result = asyncio.run(service.validate_key(raw))
        assert result is None


class TestRevokeKey:
    def test_revoke_existing_key(self, service):
        key, _ = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        success = asyncio.run(service.revoke_key(key.key_id))
        assert success is True

    def test_revoke_nonexistent_returns_false(self, service):
        success = asyncio.run(service.revoke_key("nonexistent"))
        assert success is False


class TestRotateKey:
    def test_rotate_returns_new_key(self, service):
        key, _ = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        result = asyncio.run(
            service.rotate_key(key.key_id, "admin")
        )
        assert result is not None
        new_key, new_raw = result
        assert new_key.key_id != key.key_id
        assert new_key.project_id == key.project_id
        assert new_key.scopes == key.scopes
        assert new_raw.startswith(PREFIX)

    def test_old_key_revoked_after_rotation(self, service):
        key, old_raw = asyncio.run(
            service.issue_key("proj-1", "K", ["chat:invoke"], "admin")
        )
        asyncio.run(service.rotate_key(key.key_id, "admin"))
        result = asyncio.run(service.validate_key(old_raw))
        assert result is None


class TestListKeys:
    def test_list_returns_only_project_keys(self, service):
        asyncio.run(
            service.issue_key("proj-1", "K1", ["chat:invoke"], "admin")
        )
        asyncio.run(
            service.issue_key("proj-2", "K2", ["chat:invoke"], "admin")
        )
        keys = asyncio.run(service.list_keys("proj-1"))
        assert len(keys) == 1
        assert keys[0].project_id == "proj-1"
