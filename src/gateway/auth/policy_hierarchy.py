"""Hierarchical policy resolution — org > business_unit > project > environment.

Key invariant: a child node can never exceed its parent's limits.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from src.gateway.models import PolicyNode, ResolvedPolicy

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

NODE_TYPES_ORDERED = ("org", "business_unit", "project", "environment")


class PolicyHierarchyResolver:
    """Resolves effective policy by walking from leaf to root.

    At each level, the most restrictive value wins:
    - Numeric limits: min(parent, child)
    - Model lists: intersection
    """

    def __init__(
        self, persistence: DynamoPersistence, cache_ttl_seconds: int = 300
    ) -> None:
        self._persistence = persistence
        self._cache: dict[str, tuple[ResolvedPolicy, float]] = {}
        self._cache_ttl = cache_ttl_seconds
        self._nodes: dict[str, PolicyNode] = {}

    async def load_nodes(self) -> None:
        """Load all policy nodes from persistence into memory."""
        nodes = await self._persistence.load_all_policy_nodes()
        self._nodes = {n.node_id: n for n in nodes}

    async def resolve(
        self, project_id: str, environment: str | None = None
    ) -> ResolvedPolicy:
        """Walk from leaf to root, merging limits. Cached by project+env."""
        cache_key = f"{project_id}:{environment or ''}"
        cached = self._cache.get(cache_key)
        if cached:
            policy, ts = cached
            if (time.time() - ts) < self._cache_ttl:
                return policy

        ancestry = await self.get_ancestry(project_id, environment)
        policy = self._resolve_ancestry(ancestry)

        self._cache[cache_key] = (policy, time.time())
        return policy

    async def get_ancestry(
        self, node_id: str, environment: str | None = None
    ) -> list[PolicyNode]:
        """Return ancestry path [root, ..., leaf] (top-down for merge order)."""
        if not self._nodes:
            await self.load_nodes()

        path: list[PolicyNode] = []
        current_id: str | None = node_id

        # If environment specified, look for env node first
        if environment:
            env_id = f"{node_id}:{environment}"
            if env_id in self._nodes:
                current_id = env_id

        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            node = self._nodes.get(current_id)
            if node is None:
                break
            path.append(node)
            current_id = node.parent_id

        path.reverse()  # root first
        return path

    def _resolve_ancestry(self, ancestry: list[PolicyNode]) -> ResolvedPolicy:
        """Merge limits top-down (root first). Child narrows, never expands."""
        policy = ResolvedPolicy()

        for node in ancestry:
            policy = self._merge(policy, node)

        return policy

    def _merge(self, current: ResolvedPolicy, node: PolicyNode) -> ResolvedPolicy:
        """Merge node limits into current policy (most restrictive wins)."""
        limits = node.limits

        # Rate limit: min
        node_rpm = limits.get("rate_limit_rpm")
        if node_rpm is not None:
            if current.rate_limit_rpm is None:
                current.rate_limit_rpm = node_rpm
            else:
                current.rate_limit_rpm = min(current.rate_limit_rpm, node_rpm)

        # Budget: min
        node_budget = limits.get("budget_limit")
        if node_budget is not None:
            if current.budget_limit is None:
                current.budget_limit = node_budget
            else:
                current.budget_limit = min(current.budget_limit, node_budget)

        # Max tokens: min
        node_max_tokens = limits.get("max_tokens_per_request")
        if node_max_tokens is not None:
            if current.max_tokens_per_request is None:
                current.max_tokens_per_request = node_max_tokens
            else:
                current.max_tokens_per_request = min(
                    current.max_tokens_per_request, node_max_tokens
                )

        # Allowed models: intersection
        node_models = limits.get("allowed_models")
        if node_models is not None:
            if current.allowed_models is None:
                current.allowed_models = list(node_models)
            else:
                current.allowed_models = [
                    m for m in current.allowed_models if m in node_models
                ]

        # Allowed providers: intersection
        node_providers = limits.get("allowed_providers")
        if node_providers is not None:
            if current.allowed_providers is None:
                current.allowed_providers = list(node_providers)
            else:
                current.allowed_providers = [
                    p for p in current.allowed_providers if p in node_providers
                ]

        # PII redaction: once enabled by a parent, children cannot disable
        if limits.get("pii_redaction_enabled"):
            current.pii_redaction_enabled = True

        # PII types: union (child can add types but never remove parent's)
        node_pii_types = limits.get("pii_redact_types")
        if node_pii_types is not None:
            if current.pii_redact_types is None:
                current.pii_redact_types = list(node_pii_types)
            else:
                merged = set(current.pii_redact_types) | set(node_pii_types)
                current.pii_redact_types = sorted(merged)

        return current

    async def set_node(self, node: PolicyNode) -> None:
        """Create or update a policy node. Invalidates relevant cache."""
        violations = await self.validate_node_limits(node)
        if violations:
            raise ValueError(
                f"Node '{node.node_id}' exceeds parent limits: {violations}"
            )

        await self._persistence.save_policy_node(node)
        self._nodes[node.node_id] = node
        self._invalidate_cache_for(node.node_id)

    async def validate_node_limits(self, node: PolicyNode) -> list[str]:
        """Validate that node limits don't exceed parent. Returns violations."""
        if node.parent_id is None:
            return []

        if not self._nodes:
            await self.load_nodes()

        parent = self._nodes.get(node.parent_id)
        if parent is None:
            return []

        violations: list[str] = []
        parent_limits = parent.limits
        node_limits = node.limits

        # Check numeric limits (child must be <= parent)
        for field in ("rate_limit_rpm", "budget_limit", "max_tokens_per_request"):
            parent_val = parent_limits.get(field)
            node_val = node_limits.get(field)
            if parent_val is not None and node_val is not None:
                if node_val > parent_val:
                    violations.append(
                        f"{field}: {node_val} exceeds parent limit {parent_val}"
                    )

        # Check model list (child must be subset of parent)
        parent_models = parent_limits.get("allowed_models")
        node_models = node_limits.get("allowed_models")
        if parent_models is not None and node_models is not None:
            extra = set(node_models) - set(parent_models)
            if extra:
                violations.append(
                    f"allowed_models: {extra} not in parent's allowed models"
                )

        return violations

    def _invalidate_cache_for(self, node_id: str) -> None:
        """Invalidate all cache entries.

        A change to any node (especially org/BU level) can affect many
        downstream resolved policies. Clearing the entire cache is safe
        since entries rebuild on next resolve() call.
        """
        self._cache.clear()
