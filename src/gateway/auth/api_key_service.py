"""API Key management — issue, validate, revoke, rotate."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.gateway.models import APIKey

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

PREFIX = "axon_"


CACHE_TTL_SECONDS = 300

# How often an instance checks whether another instance revoked a key. This is
# the actual upper bound on how long a revoked key keeps working elsewhere;
# CACHE_TTL_SECONDS is only the bound when the epoch check is unavailable (no
# persistence, or the read failed). Five seconds of one small point read per
# instance, against 300 seconds of a revoked credential still being accepted.
REVOCATION_POLL_SECONDS = 5


class APIKeyService:
    """Manages the lifecycle of project-scoped API keys.

    Keys are stored as SHA-256 hashes — the plaintext is returned only once
    at issue time and never persisted.
    """

    def __init__(self, persistence: DynamoPersistence) -> None:
        self._persistence = persistence
        self._cache: dict[str, tuple[APIKey, float]] = {}
        # Fallback store used only when persistence is disabled (local dev and
        # demo mode). Without it every DynamoPersistence write is a no-op and
        # every read returns empty, so a key could be issued but never listed,
        # revoked, or rotated. When persistence is enabled this stays empty and
        # DynamoDB remains the single source of truth.
        self._memory_store: dict[str, APIKey] = {}
        # Last revocation epoch this instance saw, and when it last looked. None
        # means "never successfully read", which is why the first check adopts
        # whatever it finds rather than treating a non-zero epoch as a change and
        # clearing a cache it only just built.
        self._revocation_epoch: int | None = None
        self._revocation_checked_at: float = 0.0

    @property
    def _in_memory(self) -> bool:
        return not self._persistence.enabled

    @staticmethod
    def generate_raw_key() -> str:
        return PREFIX + secrets.token_hex(32)

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    async def issue_key(
        self,
        project_id: str,
        name: str,
        scopes: list[str],
        created_by: str,
        expires_at: datetime | None = None,
    ) -> tuple[APIKey, str]:
        """Issue a new API key. Returns (key_record, raw_key_one_time)."""
        raw_key = self.generate_raw_key()
        key_hash = self.hash_key(raw_key)
        key_id = f"axk_{secrets.token_hex(12)}"

        key = APIKey(
            key_id=key_id,
            key_hash=key_hash,
            project_id=project_id,
            name=name,
            scopes=scopes,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )

        await self._persistence.save_api_key(key)
        if self._in_memory:
            self._memory_store[key_id] = key
        self._cache[key_hash] = (key, time.time())
        return key, raw_key

    async def _check_revocations(self) -> None:
        """Drop the local cache if another instance has revoked a key.

        ``revoke_key`` clears the cache on the instance that served the request,
        which is the only instance that needs no help. Every other instance kept
        serving the revoked key until its own entry aged out — up to
        ``CACHE_TTL_SECONDS`` of a credential an operator had deliberately
        revoked. With the shipped ``desired_count=2`` that was the common case,
        not an edge one.

        Polled here rather than pushed because there is no bus between instances,
        and checked at most every ``REVOCATION_POLL_SECONDS`` so the cost is one
        small point read per interval instead of one per request — the cache
        exists to keep DynamoDB off the hot path, and reading the epoch every
        request would give that back.

        A failed read leaves the epoch untouched, so behaviour degrades to the
        TTL rather than to clearing the cache on every request.
        """
        if self._in_memory:
            return
        now = time.time()
        if now - self._revocation_checked_at < REVOCATION_POLL_SECONDS:
            return
        self._revocation_checked_at = now
        epoch = await self._persistence.get_revocation_epoch()
        if epoch is None:
            return
        if self._revocation_epoch is None:
            self._revocation_epoch = epoch
            return
        if epoch != self._revocation_epoch:
            self._revocation_epoch = epoch
            self._cache.clear()

    async def validate_key(self, raw_key: str) -> APIKey | None:
        """Validate a raw API key. Returns the key record or None."""
        if not raw_key.startswith(PREFIX):
            return None

        key_hash = self.hash_key(raw_key)

        await self._check_revocations()

        entry = self._cache.get(key_hash)
        if entry is not None:
            cached, cached_at = entry
            if (time.time() - cached_at) < CACHE_TTL_SECONDS and not cached.revoked:
                if cached.expires_at and cached.expires_at < datetime.now(timezone.utc):
                    return None
                cached.last_used_at = datetime.now(timezone.utc)
                return cached
            else:
                del self._cache[key_hash]

        if self._in_memory:
            key = next(
                (k for k in self._memory_store.values() if k.key_hash == key_hash), None
            )
        else:
            key = await self._persistence.get_api_key_by_hash(key_hash)
        if key is None:
            return None

        if key.revoked:
            return None

        if key.expires_at and key.expires_at < datetime.now(timezone.utc):
            return None

        key.last_used_at = datetime.now(timezone.utc)
        self._cache[key_hash] = (key, time.time())
        return key

    async def revoke_key(self, key_id: str) -> bool:
        """Revoke a key by ID. Returns True if found and revoked."""
        key = await self._get_key(key_id)
        if key is None:
            return False

        key.revoked = True
        key.revoked_at = datetime.now(timezone.utc)
        await self._persistence.update_api_key(key)

        self._cache.pop(key.key_hash, None)
        # Tell the other instances. Without this the key stays valid on every
        # instance except this one until their cache entries expire.
        await self._persistence.bump_revocation_epoch()
        return True

    def invalidate_cache(self) -> None:
        """Clear the key cache (e.g., after receiving a revocation from another instance)."""
        self._cache.clear()

    async def rotate_key(
        self, key_id: str, rotated_by: str
    ) -> tuple[APIKey, str] | None:
        """Revoke old key and issue a new one with the same project/scopes."""
        old_key = await self._get_key(key_id)
        if old_key is None:
            return None

        await self.revoke_key(key_id)

        return await self.issue_key(
            project_id=old_key.project_id,
            name=old_key.name,
            scopes=old_key.scopes,
            created_by=rotated_by,
            expires_at=old_key.expires_at,
        )

    async def list_keys(self, project_id: str) -> list[APIKey]:
        """List all keys for a project (excludes raw key values)."""
        if self._in_memory:
            return [k for k in self._memory_store.values() if k.project_id == project_id]
        return await self._persistence.list_api_keys_for_project(project_id)

    async def _get_key(self, key_id: str) -> APIKey | None:
        """Fetch a key by ID from whichever store is authoritative."""
        if self._in_memory:
            return self._memory_store.get(key_id)
        return await self._persistence.get_api_key(key_id)
