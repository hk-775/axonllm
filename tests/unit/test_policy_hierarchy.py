"""Tests for hierarchical policy resolution."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import src.gateway.auth.policy_hierarchy as policy_hierarchy_module
from src.gateway.auth.policy_hierarchy import (
    PolicyHierarchyResolver,
    PolicyHierarchyStoreUnavailable,
    PolicyHierarchyWriteConflict,
)
from src.gateway.models import PolicyNode, Project, ResolvedPolicy
from src.gateway.persistence import PersistenceConflictError


class FakePersistence:
    """In-memory persistence for testing."""

    def __init__(self):
        self._nodes: dict[str, PolicyNode] = {}
        self._tenant_nodes: dict[str, dict[str, PolicyNode]] = {}
        self._tenant_revisions: dict[str, int] = {}
        self._enabled = True
        self.revision_reads = 0
        self.snapshot_reads = 0
        self.save_calls: list[tuple[str, str, int, bool]] = []
        self.fail_revision_reads = False
        self.fail_snapshot_reads = False

    @property
    def enabled(self):
        return self._enabled

    async def save_policy_node(self, node: PolicyNode) -> None:
        self._nodes[node.node_id] = node

    async def get_policy_node(self, node_id: str) -> PolicyNode | None:
        return self._nodes.get(node_id)

    async def load_all_policy_nodes(self) -> list[PolicyNode]:
        return list(self._nodes.values())

    async def save_tenant_policy_node(
        self,
        tenant_id: str,
        node: PolicyNode,
        *,
        expected_revision: int | None = None,
        create_only: bool | None = None,
    ) -> int:
        scoped_nodes = self._tenant_nodes.setdefault(tenant_id, {})
        current_revision = self._tenant_revisions.get(tenant_id, 0)
        if expected_revision is None:
            expected_revision = current_revision
        if create_only is None:
            create_only = node.node_id not in scoped_nodes
        if (
            expected_revision != current_revision
            or create_only == (node.node_id in scoped_nodes)
        ):
            raise PersistenceConflictError("hierarchy changed")

        scoped_nodes[node.node_id] = deepcopy(node)
        next_revision = current_revision + 1
        self._tenant_revisions[tenant_id] = next_revision
        self.save_calls.append(
            (tenant_id, node.node_id, expected_revision, create_only)
        )
        return next_revision

    async def get_tenant_policy_hierarchy_revision(
        self,
        tenant_id: str,
    ) -> int:
        self.revision_reads += 1
        if self.fail_revision_reads:
            raise RuntimeError("revision unavailable")
        return self._tenant_revisions.get(tenant_id, 0)

    async def load_tenant_policy_nodes_snapshot(
        self,
        tenant_id: str,
    ) -> tuple[list[PolicyNode], int]:
        self.snapshot_reads += 1
        if self.fail_snapshot_reads:
            raise RuntimeError("snapshot unavailable")
        return (
            deepcopy(list(self._tenant_nodes.get(tenant_id, {}).values())),
            self._tenant_revisions.get(tenant_id, 0),
        )

    async def load_tenant_policy_nodes(
        self,
        tenant_id: str,
    ) -> list[PolicyNode] | None:
        return deepcopy(
            list(self._tenant_nodes.get(tenant_id, {}).values())
        )


def _run(coro):
    return asyncio.run(coro)


def _controlled_clock(monkeypatch):
    clock = {"now": 0.0}
    wall_time = policy_hierarchy_module.time.time()
    monkeypatch.setattr(
        policy_hierarchy_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock["now"],
            time=lambda: wall_time,
        ),
    )
    return clock


def _tenant_policy_client(resolver, tenant_id="tenant-a"):
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient

    from src.gateway.admin.policy_routes import (
        PolicyHierarchyAPI,
        create_policy_hierarchy_routes,
    )

    app = Starlette(
        routes=create_policy_hierarchy_routes(
            PolicyHierarchyAPI(resolver=resolver)
        )
    )

    async def tenant_context(request, call_next):
        request.state.context = SimpleNamespace(tenant_id=tenant_id)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=tenant_context)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def persistence():
    return FakePersistence()


@pytest.fixture
def resolver(persistence):
    return PolicyHierarchyResolver(persistence=persistence, cache_ttl_seconds=0)


class TestSingleNode:
    def test_org_node_resolves_own_limits(self, resolver, persistence):
        node = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme Corp",
            limits={"rate_limit_rpm": 1000, "budget_limit": 50000.0},
        )
        _run(persistence.save_policy_node(node))
        policy = _run(resolver.resolve("org:acme"))

        assert policy.rate_limit_rpm == 1000
        assert policy.budget_limit == 50000.0

    def test_unknown_node_returns_empty_policy(self, resolver):
        policy = _run(resolver.resolve("nonexistent"))
        assert policy.rate_limit_rpm is None
        assert policy.budget_limit is None


class TestTwoLevelHierarchy:
    def test_child_inherits_parent_limits(self, resolver, persistence):
        org = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme",
            limits={"rate_limit_rpm": 1000, "budget_limit": 50000.0},
        )
        project = PolicyNode(
            node_id="proj:ml-team",
            node_type="project",
            parent_id="org:acme",
            display_name="ML Team",
            limits={"rate_limit_rpm": 500},
        )
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(project))

        policy = _run(resolver.resolve("proj:ml-team"))

        assert policy.rate_limit_rpm == 500  # min(1000, 500)
        assert policy.budget_limit == 50000.0  # inherited from parent

    def test_child_cannot_exceed_parent(self, resolver, persistence):
        org = PolicyNode(
            node_id="org:acme",
            node_type="org",
            parent_id=None,
            display_name="Acme",
            limits={"rate_limit_rpm": 100},
        )
        project = PolicyNode(
            node_id="proj:ml",
            node_type="project",
            parent_id="org:acme",
            display_name="ML",
            limits={"rate_limit_rpm": 200},  # exceeds parent
        )
        _run(persistence.save_policy_node(org))

        violations = _run(resolver.validate_node_limits(project))
        assert len(violations) == 1
        assert "rate_limit_rpm" in violations[0]


class TestFourLevelHierarchy:
    def test_full_hierarchy_most_restrictive_wins(self, resolver, persistence):
        nodes = [
            PolicyNode("org:acme", "org", None, "Acme",
                       limits={"rate_limit_rpm": 10000, "budget_limit": 100000.0,
                               "allowed_models": ["claude-opus", "claude-sonnet", "gpt-4o"]}),
            PolicyNode("bu:engineering", "business_unit", "org:acme", "Engineering",
                       limits={"rate_limit_rpm": 5000, "budget_limit": 50000.0,
                               "allowed_models": ["claude-opus", "claude-sonnet", "gpt-4o"]}),
            PolicyNode("proj:ml-platform", "project", "bu:engineering", "ML Platform",
                       limits={"rate_limit_rpm": 2000,
                               "allowed_models": ["claude-opus", "claude-sonnet"]}),
            PolicyNode("proj:ml-platform:prod", "environment", "proj:ml-platform", "Production",
                       limits={"rate_limit_rpm": 1000, "max_tokens_per_request": 4096}),
        ]
        for n in nodes:
            _run(persistence.save_policy_node(n))

        policy = _run(resolver.resolve("proj:ml-platform:prod"))

        assert policy.rate_limit_rpm == 1000
        assert policy.budget_limit == 50000.0
        assert set(policy.allowed_models) == {"claude-opus", "claude-sonnet"}
        assert policy.max_tokens_per_request == 4096


class TestModelIntersection:
    def test_allowed_models_intersected(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"allowed_models": ["a", "b", "c"]})
        proj = PolicyNode("proj:y", "project", "org:x", "Y",
                          limits={"allowed_models": ["b", "c", "d"]})
        _run(persistence.save_policy_node(org))
        _run(persistence.save_policy_node(proj))

        policy = _run(resolver.resolve("proj:y"))
        assert set(policy.allowed_models) == {"b", "c"}

    def test_child_models_subset_of_parent(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"allowed_models": ["a", "b"]})
        proj = PolicyNode("proj:y", "project", "org:x", "Y",
                          limits={"allowed_models": ["a", "b", "c"]})
        _run(persistence.save_policy_node(org))

        violations = _run(resolver.validate_node_limits(proj))
        assert len(violations) == 1
        assert "allowed_models" in violations[0]


class TestCacheInvalidation:
    def test_cache_invalidated_on_update(self, resolver, persistence):
        resolver._cache_ttl = 9999  # long TTL
        node = PolicyNode("org:x", "org", None, "X",
                          limits={"rate_limit_rpm": 500})
        _run(persistence.save_policy_node(node))

        # First resolve caches
        policy1 = _run(resolver.resolve("org:x"))
        assert policy1.rate_limit_rpm == 500

        # Update node
        node.limits["rate_limit_rpm"] = 200
        _run(resolver.set_node(node))

        # Should pick up new value (cache invalidated)
        policy2 = _run(resolver.resolve("org:x"))
        assert policy2.rate_limit_rpm == 200


class TestSetNodeValidation:
    def test_set_node_rejects_exceeding_parent(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"budget_limit": 1000.0})
        _run(persistence.save_policy_node(org))
        _run(resolver.load_nodes())

        child = PolicyNode("proj:y", "project", "org:x", "Y",
                           limits={"budget_limit": 2000.0})  # exceeds parent

        with pytest.raises(ValueError, match="exceeds parent"):
            _run(resolver.set_node(child))

    def test_set_node_accepts_valid_child(self, resolver, persistence):
        org = PolicyNode("org:x", "org", None, "X",
                         limits={"budget_limit": 1000.0})
        _run(persistence.save_policy_node(org))
        _run(resolver.load_nodes())

        child = PolicyNode("proj:y", "project", "org:x", "Y",
                           limits={"budget_limit": 500.0})
        _run(resolver.set_node(child))  # should not raise

        assert "proj:y" in resolver._nodes


class TestTenantPolicyIsolation:
    def test_identical_node_ids_resolve_within_their_tenant(
        self,
        resolver,
        persistence,
    ):
        tenant_a = PolicyNode(
            "same-project",
            "project",
            None,
            "Tenant A",
            limits={"rate_limit_rpm": 10, "budget_limit": 100.0},
        )
        tenant_b = PolicyNode(
            "same-project",
            "project",
            None,
            "Tenant B",
            limits={"rate_limit_rpm": 20, "budget_limit": 200.0},
        )
        _run(persistence.save_tenant_policy_node("tenant-a", tenant_a))
        _run(persistence.save_tenant_policy_node("tenant-b", tenant_b))

        policy_a = _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-a",
        ))
        policy_b = _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-b",
        ))

        assert policy_a.rate_limit_rpm == 10
        assert policy_a.budget_limit == 100.0
        assert policy_b.rate_limit_rpm == 20
        assert policy_b.budget_limit == 200.0

    def test_tenant_lookup_never_falls_back_to_legacy_nodes(
        self,
        resolver,
        persistence,
    ):
        _run(persistence.save_policy_node(PolicyNode(
            "same-project",
            "project",
            None,
            "Legacy",
            limits={"budget_limit": 999.0},
        )))

        tenant_policy = _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-a",
        ))
        legacy_policy = _run(resolver.resolve("same-project"))

        assert tenant_policy.budget_limit is None
        assert legacy_policy.budget_limit == 999.0

    def test_cache_entries_are_tenant_qualified(
        self,
        resolver,
        persistence,
    ):
        resolver._cache_ttl = 9999
        for tenant_id, limit in (("tenant-a", 10), ("tenant-b", 20)):
            _run(persistence.save_tenant_policy_node(
                tenant_id,
                PolicyNode(
                    "same-project",
                    "project",
                    None,
                    tenant_id,
                    limits={"rate_limit_rpm": limit},
                ),
            ))

        assert _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-a",
        )).rate_limit_rpm == 10
        assert _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-b",
        )).rate_limit_rpm == 20
        assert len(resolver._cache) == 2

    def test_tenant_update_invalidates_only_its_policy(
        self,
        resolver,
        persistence,
    ):
        resolver._cache_ttl = 9999
        for tenant_id, limit in (("tenant-a", 10), ("tenant-b", 20)):
            _run(resolver.set_node(
                PolicyNode(
                    "same-project",
                    "project",
                    None,
                    tenant_id,
                    limits={"rate_limit_rpm": limit},
                ),
                tenant_id=tenant_id,
            ))
            _run(resolver.resolve(
                "same-project",
                tenant_id=tenant_id,
            ))

        _run(resolver.set_node(
            PolicyNode(
                "same-project",
                "project",
                None,
                "tenant-a",
                limits={"rate_limit_rpm": 5},
            ),
            tenant_id="tenant-a",
        ))

        assert _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-a",
        )).rate_limit_rpm == 5
        assert _run(resolver.resolve(
            "same-project",
            tenant_id="tenant-b",
        )).rate_limit_rpm == 20

    def test_updating_root_hydrates_and_retains_persisted_descendants(
        self,
        resolver,
        persistence,
    ):
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "org",
                "org",
                None,
                "Org",
                limits={"budget_limit": 100.0},
            ),
        ))
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                "org",
                "Project",
                limits={"rate_limit_rpm": 10},
            ),
        ))

        _run(resolver.set_node(
            PolicyNode(
                "org",
                "org",
                None,
                "Org",
                limits={"budget_limit": 80.0},
            ),
            tenant_id="tenant-a",
        ))
        policy = _run(resolver.resolve(
            "project",
            tenant_id="tenant-a",
        ))

        assert policy.budget_limit == 80.0
        assert policy.rate_limit_rpm == 10

    def test_empty_tenant_id_cannot_select_legacy_namespace(self, resolver):
        with pytest.raises(ValueError, match="tenant_id"):
            _run(resolver.resolve("project", tenant_id=""))


class TestTenantPolicyRevisionPolling:
    def test_cached_resolve_polls_only_after_five_seconds(
        self,
        resolver,
        persistence,
        monkeypatch,
    ):
        clock = _controlled_clock(monkeypatch)
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 10},
            ),
        ))
        resolver._cache_ttl = 9999

        assert _run(resolver.resolve(
            "project",
            tenant_id="tenant-a",
        )).rate_limit_rpm == 10
        assert persistence.snapshot_reads == 1
        assert persistence.revision_reads == 0

        clock["now"] = 4.999
        _run(resolver.resolve("project", tenant_id="tenant-a"))
        assert persistence.revision_reads == 0

        clock["now"] = 5.0
        _run(resolver.resolve("project", tenant_id="tenant-a"))
        assert persistence.revision_reads == 1
        assert persistence.snapshot_reads == 1

    def test_changed_revision_refreshes_cached_resolution(
        self,
        resolver,
        persistence,
        monkeypatch,
    ):
        clock = _controlled_clock(monkeypatch)
        original = PolicyNode(
            "project",
            "project",
            None,
            "Project",
            limits={"rate_limit_rpm": 10},
        )
        _run(persistence.save_tenant_policy_node("tenant-a", original))
        resolver._cache_ttl = 9999
        assert _run(resolver.resolve(
            "project",
            tenant_id="tenant-a",
        )).rate_limit_rpm == 10

        replacement = PolicyNode(
            "project",
            "project",
            None,
            "Project",
            limits={"rate_limit_rpm": 5},
        )
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            replacement,
            expected_revision=1,
            create_only=False,
        ))
        clock["now"] = 5.0

        refreshed = _run(resolver.resolve(
            "project",
            tenant_id="tenant-a",
        ))

        assert refreshed.rate_limit_rpm == 5
        assert resolver.known_revision("tenant-a") == 2
        assert persistence.snapshot_reads == 2

    def test_validation_refreshes_parent_before_checking_child(
        self,
        resolver,
        persistence,
        monkeypatch,
    ):
        clock = _controlled_clock(monkeypatch)
        parent = PolicyNode(
            "org",
            "org",
            None,
            "Org",
            limits={"budget_limit": 100.0},
        )
        _run(persistence.save_tenant_policy_node("tenant-a", parent))
        child = PolicyNode(
            "project",
            "project",
            "org",
            "Project",
            limits={"budget_limit": 80.0},
        )
        assert _run(resolver.validate_node_limits(
            child,
            tenant_id="tenant-a",
        )) == []

        parent.limits["budget_limit"] = 50.0
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            parent,
            expected_revision=1,
            create_only=False,
        ))
        clock["now"] = 5.0

        violations = _run(resolver.validate_node_limits(
            child,
            tenant_id="tenant-a",
        ))

        assert len(violations) == 1
        assert "exceeds parent limit 50.0" in violations[0]

    def test_concurrent_refreshes_are_single_flight(
        self,
        persistence,
        monkeypatch,
    ):
        clock = _controlled_clock(monkeypatch)
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 10},
            ),
        ))
        resolver = PolicyHierarchyResolver(
            persistence,
            cache_ttl_seconds=9999,
        )

        async def exercise():
            await resolver.resolve("project", tenant_id="tenant-a")
            clock["now"] = 5.0
            started = asyncio.Event()
            release = asyncio.Event()
            original_get_revision = (
                persistence.get_tenant_policy_hierarchy_revision
            )

            async def blocked_revision(tenant_id):
                started.set()
                await release.wait()
                return await original_get_revision(tenant_id)

            persistence.get_tenant_policy_hierarchy_revision = (
                blocked_revision
            )
            requests = [
                asyncio.create_task(
                    resolver.resolve("project", tenant_id="tenant-a")
                )
                for _ in range(20)
            ]
            await started.wait()
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*requests)

        _run(exercise())

        assert persistence.revision_reads == 1
        assert resolver._tenant_refresh_tasks == {}

    def test_failed_later_poll_keeps_last_known_good_and_retries(
        self,
        resolver,
        persistence,
        monkeypatch,
    ):
        clock = _controlled_clock(monkeypatch)
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 10},
            ),
        ))
        resolver._cache_ttl = 9999
        _run(resolver.resolve("project", tenant_id="tenant-a"))
        initial_poll_time = resolver._tenant_last_revision_check["tenant-a"]

        persistence.fail_revision_reads = True
        clock["now"] = 5.0
        first = _run(resolver.resolve("project", tenant_id="tenant-a"))
        second = _run(resolver.resolve("project", tenant_id="tenant-a"))

        assert first.rate_limit_rpm == 10
        assert second.rate_limit_rpm == 10
        assert resolver.known_revision("tenant-a") == 1
        assert (
            resolver._tenant_last_revision_check["tenant-a"]
            == initial_poll_time
        )
        assert persistence.revision_reads == 2


class TestTenantPolicyCAS:
    def test_create_and_update_use_the_loaded_hierarchy_revision(
        self,
        resolver,
        persistence,
    ):
        node = PolicyNode(
            "project",
            "project",
            None,
            "Project",
            limits={"rate_limit_rpm": 10},
        )

        assert _run(resolver.set_node(
            node,
            tenant_id="tenant-a",
        )) == 1
        node.limits["rate_limit_rpm"] = 5
        assert _run(resolver.set_node(
            node,
            tenant_id="tenant-a",
        )) == 2

        assert persistence.save_calls == [
            ("tenant-a", "project", 0, True),
            ("tenant-a", "project", 1, False),
        ]
        assert resolver.known_revision("tenant-a") == 2

    def test_two_resolvers_cannot_overwrite_the_same_revision(
        self,
        persistence,
    ):
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 10},
            ),
        ))
        first = PolicyHierarchyResolver(persistence)
        second = PolicyHierarchyResolver(persistence)
        _run(first.resolve("project", tenant_id="tenant-a"))
        _run(second.resolve("project", tenant_id="tenant-a"))

        _run(first.set_node(
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 5},
            ),
            tenant_id="tenant-a",
            create_only=False,
        ))
        with pytest.raises(PolicyHierarchyWriteConflict):
            _run(second.set_node(
                PolicyNode(
                    "project",
                    "project",
                    None,
                    "Project",
                    limits={"rate_limit_rpm": 2},
                ),
                tenant_id="tenant-a",
                create_only=False,
            ))

        assert (
            second._tenant_nodes["tenant-a"]["project"]
            .limits["rate_limit_rpm"]
            == 10
        )
        assert second.known_revision("tenant-a") == 1
        assert "tenant-a" not in second._tenant_last_revision_check

    def test_create_and_update_modes_are_distinct(
        self,
        resolver,
        persistence,
    ):
        node = PolicyNode("project", "project", None, "Project", limits={})
        _run(resolver.set_node(
            node,
            tenant_id="tenant-a",
            create_only=True,
        ))

        with pytest.raises(PolicyHierarchyWriteConflict):
            _run(resolver.set_node(
                node,
                tenant_id="tenant-a",
                create_only=True,
            ))

        missing = PolicyNode("other", "project", None, "Other", limits={})
        with pytest.raises(PolicyHierarchyWriteConflict):
            _run(resolver.set_node(
                missing,
                tenant_id="tenant-a",
                create_only=False,
            ))

        assert list(persistence._tenant_nodes["tenant-a"]) == ["project"]


class TestTenantPolicyPersistenceFailures:
    def test_missing_tenant_read_contract_fails_closed(self):
        class LegacyOnlyPersistence:
            enabled = True

            async def load_all_policy_nodes(self):
                return []

        resolver = PolicyHierarchyResolver(LegacyOnlyPersistence())

        with pytest.raises(PolicyHierarchyStoreUnavailable):
            _run(resolver.resolve("project", tenant_id="tenant-a"))

    def test_failed_initial_snapshot_fails_closed(self, persistence):
        persistence.fail_snapshot_reads = True
        resolver = PolicyHierarchyResolver(persistence)

        with pytest.raises(PolicyHierarchyStoreUnavailable):
            _run(resolver.resolve("project", tenant_id="tenant-a"))
        assert "tenant-a" not in resolver._loaded_tenants

    def test_failed_tenant_write_is_not_adopted_in_memory(
        self,
        resolver,
        persistence,
    ):
        async def fail_save(tenant_id, node):
            return False

        persistence.save_tenant_policy_node = fail_save
        node = PolicyNode(
            "project",
            "project",
            None,
            "Project",
            limits={},
        )

        with pytest.raises(PolicyHierarchyStoreUnavailable):
            _run(resolver.set_node(node, tenant_id="tenant-a"))
        assert "project" not in resolver._tenant_nodes.get("tenant-a", {})


class TestCanonicalProjectPolicyLimit:
    def test_canonical_project_rpm_is_applied_and_tenant_is_inferred(
        self,
        resolver,
        persistence,
    ):
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 50},
            ),
        ))
        project = Project(
            project_id="project",
            name="Project",
            tenant_id="tenant-a",
            rate_limit_rpm=10,
        )

        policy = _run(resolver.resolve("project", project=project))

        assert policy.rate_limit_rpm == 10

    def test_canonical_project_does_not_mutate_cached_hierarchy(
        self,
        resolver,
        persistence,
    ):
        resolver._cache_ttl = 9999
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "project",
                "project",
                None,
                "Project",
                limits={"rate_limit_rpm": 50},
            ),
        ))
        canonical = Project(
            project_id="project",
            name="Project",
            tenant_id="tenant-a",
            rate_limit_rpm=10,
        )

        assert _run(resolver.resolve(
            "project",
            project=canonical,
        )).rate_limit_rpm == 10
        assert _run(resolver.resolve(
            "project",
            tenant_id="tenant-a",
        )).rate_limit_rpm == 50


class TestCreateNodeRejectsAMalformedBody:
    """A missing required field is a 400, not a 500.

    `create_node` read `body["node_id"]` / `body["node_type"]` directly and
    caught only the `ValueError` that `set_node` raises, so a partial body raised
    `KeyError` and reached the client as a 500. Every sibling admin POST
    (`/admin/projects`, `/admin/models`, `/admin/webhooks`,
    `/admin/regions/spokes`) answers 400 for the same input — this route was the
    only one that didn't, and it is reachable by any `admin:*` holder.
    """

    @pytest.fixture
    def client(self, resolver):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from src.gateway.admin.policy_routes import (
            PolicyHierarchyAPI,
            create_policy_hierarchy_routes,
        )

        app = Starlette(
            routes=create_policy_hierarchy_routes(PolicyHierarchyAPI(resolver=resolver))
        )
        # raise_server_exceptions=False so an unhandled exception shows up as the
        # 500 a real client would see, rather than failing the test as a raise.
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({}, id="empty"),
            pytest.param({"node_type": "org"}, id="no-node_id"),
            pytest.param({"node_id": "org:x"}, id="no-node_type"),
            pytest.param({"node_id": "", "node_type": "org"}, id="blank-node_id"),
        ],
    )
    def test_a_missing_required_field_is_a_400(self, client, body):
        resp = client.post("/admin/policies/hierarchy", json=body)

        assert resp.status_code == 400, (
            f"body={body} produced {resp.status_code}; an incomplete request from "
            "an authorized admin should not read as a server fault"
        )
        assert "message" in resp.json()["error"]

    def test_a_complete_body_still_creates_the_node(self, client):
        resp = client.post(
            "/admin/policies/hierarchy",
            json={"node_id": "org:x", "node_type": "org", "limits": {}},
        )

        assert resp.status_code == 201
        assert resp.json()["node_id"] == "org:x"


class TestTenantPolicyHierarchyAPI:
    def test_responses_expose_the_hierarchy_revision(
        self,
        resolver,
    ):
        client = _tenant_policy_client(resolver)

        created = client.post(
            "/admin/policies/hierarchy",
            json={
                "node_id": "project",
                "node_type": "project",
                "limits": {"rate_limit_rpm": 10},
            },
        )
        listed = client.get("/admin/policies/hierarchy")
        fetched = client.get("/admin/policies/hierarchy/project")
        effective = client.get("/admin/policies/effective/project")
        updated = client.put(
            "/admin/policies/hierarchy/project",
            json={"limits": {"rate_limit_rpm": 5}},
        )

        assert created.status_code == 201
        assert created.json()["revision"] == 1
        assert created.headers["etag"] == '"policy-hierarchy-1"'
        assert listed.json()[0]["revision"] == 1
        assert listed.headers["x-policy-hierarchy-revision"] == "1"
        assert fetched.json()["revision"] == 1
        assert effective.json()["revision"] == 1
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.headers["etag"] == '"policy-hierarchy-2"'

    def test_write_conflict_maps_to_409(
        self,
        resolver,
        persistence,
    ):
        client = _tenant_policy_client(resolver)
        created = client.post(
            "/admin/policies/hierarchy",
            json={"node_id": "project", "node_type": "project"},
        )
        assert created.status_code == 201

        async def conflict(*args, **kwargs):
            raise PersistenceConflictError("lost race")

        persistence.save_tenant_policy_node = conflict
        response = client.put(
            "/admin/policies/hierarchy/project",
            json={"display_name": "Changed"},
        )

        assert response.status_code == 409
        assert (
            response.json()["error"]["code"]
            == "policy_hierarchy_write_conflict"
        )
        assert (
            resolver._tenant_nodes["tenant-a"]["project"].display_name
            == "project"
        )

    def test_initial_store_outage_maps_to_503(
        self,
        persistence,
    ):
        persistence.fail_snapshot_reads = True
        client = _tenant_policy_client(
            PolicyHierarchyResolver(persistence)
        )

        response = client.get("/admin/policies/hierarchy")

        assert response.status_code == 503
        assert (
            response.json()["error"]["code"]
            == "policy_hierarchy_store_unavailable"
        )

    def test_write_store_outage_maps_to_503(
        self,
        resolver,
        persistence,
    ):
        client = _tenant_policy_client(resolver)
        loaded = client.get("/admin/policies/hierarchy")
        assert loaded.status_code == 200

        async def unavailable(*args, **kwargs):
            raise RuntimeError("store unavailable")

        persistence.save_tenant_policy_node = unavailable
        response = client.post(
            "/admin/policies/hierarchy",
            json={"node_id": "project", "node_type": "project"},
        )

        assert response.status_code == 503
        assert (
            response.json()["error"]["code"]
            == "policy_hierarchy_store_unavailable"
        )
