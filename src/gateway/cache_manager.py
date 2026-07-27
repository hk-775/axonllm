"""Response cache with per-project TTL configuration."""

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse


class CacheManager:
    """In-memory response cache with TTL-based expiration and LRU eviction."""

    MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        self._cache: OrderedDict[str, dict] = OrderedDict()

    async def get(self, cache_key: str) -> ChatCompletionResponse | None:
        """Look up cached response. Returns None on miss or if expired."""
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        if datetime.now(timezone.utc) >= entry["expires_at"]:
            del self._cache[cache_key]
            return None
        self._cache.move_to_end(cache_key)
        return entry["response"]

    async def put(
        self, cache_key: str, response: ChatCompletionResponse, ttl_seconds: int
    ) -> None:
        """Store response in cache with TTL."""
        self._cache[cache_key] = {
            "response": response,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        }
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self.MAX_ENTRIES:
            self._cache.popitem(last=False)

    def compute_cache_key(
        self, request: ChatCompletionRequest, project_id: str
    ) -> str:
        """Generate deterministic cache key from request parameters.

        Uses SHA-256 hash of (model, messages, temperature, max_tokens,
        top_p, stop, tools, tool_choice, project_id) serialized as sorted JSON.

        ``tools``/``tool_choice`` are part of the key because they change the
        answer: the same prompt sent with a tool list can return a tool call and
        sent without one returns prose. Omitting them would serve a cached
        tool-free reply to a request that needed a tool call.
        """
        key_data = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "top_p": request.top_p,
            "stop": request.stop,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "project_id": project_id,
        }
        canonical = json.dumps(key_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
