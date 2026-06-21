"""Response cache with per-project TTL configuration."""

import hashlib
import json
from datetime import datetime, timedelta

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse


class CacheManager:
    """In-memory response cache with TTL-based expiration."""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    async def get(self, cache_key: str) -> ChatCompletionResponse | None:
        """Look up cached response. Returns None on miss or if expired."""
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        if datetime.utcnow() >= entry["expires_at"]:
            del self._cache[cache_key]
            return None
        return entry["response"]

    async def put(
        self, cache_key: str, response: ChatCompletionResponse, ttl_seconds: int
    ) -> None:
        """Store response in cache with TTL."""
        self._cache[cache_key] = {
            "response": response,
            "expires_at": datetime.utcnow() + timedelta(seconds=ttl_seconds),
        }

    def compute_cache_key(
        self, request: ChatCompletionRequest, project_id: str
    ) -> str:
        """Generate deterministic cache key from request parameters.

        Uses SHA-256 hash of (model, messages, temperature, max_tokens,
        top_p, stop, project_id) serialized as sorted JSON.
        """
        key_data = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "top_p": request.top_p,
            "stop": request.stop,
            "project_id": project_id,
        }
        canonical = json.dumps(key_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
