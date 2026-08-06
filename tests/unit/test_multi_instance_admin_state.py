"""Admin state shared across instances, not trapped in one process.

`infra/stack.py` deploys `desired_count=2` and auto-scales to 10, so two
gateways behind one ALB is the shipped default rather than an exotic
configuration. Every test here is the second instance's point of view: it did
not serve the write, its startup-hydrated caches predate it, and the operator
who made the change cannot tell which task their next request will land on.

The bug these were written against was found on a live two-task deployment, not
by reading the code. Ten identical authenticated `GET /admin/projects` requests
against the ALB returned the project six times and `[]` four times, decided
purely by which task answered — a `201 Created` followed by a `[]` list, with
the project sitting in DynamoDB the whole time.

Both halves of that are covered: a project one instance created must be
resolvable on another, and a key one instance revoked must stop working on
another. The fake persistence layer is deliberately shared between two service
objects, because "two processes, one table" is the entire condition under test
and a single instance cannot exhibit either failure.
"""

from __future__ import annotations

import asyncio
import copy
import pathlib

import yaml

from src.gateway.admin.routes import AdminAPI
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import Project


class SharedTable:
    """One table, standing in for DynamoDB, shared by every instance in a test.

    Only the operations the code under test performs are implemented; anything
    else should fail loudly rather than quietly return an empty result and make
    a test pass for the wrong reason.
    """

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.keys: dict[str, object] = {}
        self.hash_index: dict[str, str] = {}
        self.epoch = 0
        self.enabled = True
        self.reads = 0
        self.key_reads = 0

    # --- projects ---

    async def save_project(self, project: Project) -> None:
        self.projects[project.project_id] = copy.deepcopy(project)

    async def get_project(self, project_id: str) -> Project | None:
        self.reads += 1
        project = self.projects.get(project_id)
        return copy.deepcopy(project) if project is not None else None

    async def load_projects(self) -> dict[str, Project]:
        self.reads += 1
        return copy.deepcopy(self.projects)

    # --- keys ---
    #
    # Reads return a *copy*, and that detail is what makes these tests mean
    # anything. A double that hands back the same object it stored gives every
    # instance a shared mutable record, so `revoke_key` mutating it is visible
    # everywhere for free and the cross-instance bug cannot reproduce — the
    # first version of this file made that mistake and passed against the
    # unfixed code. Real DynamoDB serializes on write and deserializes on read,
    # so two instances never hold the same object.

    async def save_api_key(self, key) -> None:
        self.keys[key.key_id] = copy.deepcopy(key)
        self.hash_index[key.key_hash] = key.key_id

    async def update_api_key(self, key) -> None:
        self.keys[key.key_id] = copy.deepcopy(key)

    async def get_api_key(self, key_id):
        key = self.keys.get(key_id)
        return copy.deepcopy(key) if key is not None else None

    async def get_api_key_by_hash(self, key_hash):
        self.key_reads += 1
        key_id = self.hash_index.get(key_hash)
        if key_id is None:
            return None
        key = self.keys.get(key_id)
        return copy.deepcopy(key) if key is not None else None

    # --- revocation signal ---

    async def bump_revocation_epoch(self) -> None:
        self.epoch += 1

    async def get_revocation_epoch(self) -> int | None:
        return self.epoch


def _admin(table: SharedTable, projects: dict | None = None) -> AdminAPI:
    """One gateway instance, wired to the shared table.

    `projects` is what this instance hydrated at startup — the default of empty
    models an instance that started before the project existed, which is the
    situation the bug appeared in.
    """
    pricing = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "pricing.yaml").read_text()
    )
    return AdminAPI(
        cost_tracker=CostTracker(pricing_config=pricing),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        projects=projects if projects is not None else {},
        persistence=table,
    )


class TestAProjectOneInstanceCreated:
    """The reported bug: `201 Created` on one task, `404`/`[]` on the other."""

    def test_another_instance_can_resolve_it_by_id(self):
        table = SharedTable()
        asyncio.run(table.save_project(Project(project_id="p1", name="Alpha")))

        other = _admin(table)  # started before p1 existed
        found = asyncio.run(other._get_project("p1"))

        assert found is not None, (
            "an instance that did not serve the POST returned None for a project "
            "that is in the table — this is the live bug: GET /admin/projects/p1 "
            "answered 404 on one task and 200 on another"
        )
        assert found.name == "Alpha"

    def test_another_instance_lists_it(self):
        table = SharedTable()
        asyncio.run(table.save_project(Project(project_id="p1", name="Alpha")))

        other = _admin(table)
        listed = asyncio.run(other._all_projects())

        assert "p1" in listed, "GET /admin/projects omitted a project another instance created"

    def test_a_genuinely_absent_project_is_still_absent(self):
        """The fix must not turn every 404 into a hydration attempt that succeeds.

        Without this, a resolver that returned a blank `Project` on a miss would
        satisfy every other test in this class.
        """
        table = SharedTable()
        assert asyncio.run(_admin(table)._get_project("nope")) is None

    def test_resolution_is_cached_rather_than_read_every_time(self):
        """A read-through on every request would put DynamoDB on the hot path.

        Asserted by counting reads, not by timing: the second lookup must be
        served from the instance's own dict.
        """
        table = SharedTable()
        asyncio.run(table.save_project(Project(project_id="p1", name="Alpha")))
        instance = _admin(table)

        asyncio.run(instance._get_project("p1"))
        after_first = table.reads
        asyncio.run(instance._get_project("p1"))

        assert table.reads == after_first, "second lookup re-read the table instead of using the local dict"

    def test_a_locally_mutated_project_is_not_clobbered_by_the_scan(self):
        """`_all_projects` merges; it must not overwrite an in-flight change.

        A request that is mid-mutation holds a reference to the object in
        `self.projects` and has not necessarily persisted yet. If the list
        endpoint replaced that object with the stored copy, the pending change
        would vanish — a data-loss bug introduced by the fix for a visibility
        one.
        """
        table = SharedTable()
        stored = Project(project_id="p1", name="Old Name")
        asyncio.run(table.save_project(stored))

        instance = _admin(table, projects={"p1": Project(project_id="p1", name="New Name")})
        listed = asyncio.run(instance._all_projects())

        assert listed["p1"].name == "New Name", "the scan overwrote a locally mutated project"

    def test_the_resolved_object_is_the_one_mutations_will_write(self):
        """Mutation handlers change the returned object in place.

        `update_project`, `add_member` and the model handlers all mutate what
        the resolver hands back and rely on `self.projects` holding that same
        reference (see `_persist_project`). A resolver that returned a detached
        copy would make those writes apply to an object no later request can
        see — the visible bug replaced by a silent one.
        """
        table = SharedTable()
        asyncio.run(table.save_project(Project(project_id="p1", name="Alpha")))
        instance = _admin(table)

        resolved = asyncio.run(instance._get_project("p1"))
        resolved.name = "Mutated"

        assert instance.projects["p1"] is resolved, "resolver returned a copy, so in-place writes are lost"
        assert asyncio.run(instance._get_project("p1")).name == "Mutated"

    def test_without_persistence_it_degrades_to_local_state(self):
        """No table configured is a supported single-node mode, not an error."""
        table = SharedTable()
        table.enabled = False
        instance = _admin(table, projects={"local": Project(project_id="local", name="Local")})

        assert asyncio.run(instance._get_project("local")) is not None
        assert asyncio.run(instance._get_project("remote")) is None
        assert table.reads == 0, "read the table despite persistence being disabled"

    def test_a_failed_read_does_not_take_down_the_list_endpoint(self):
        """A DynamoDB error must degrade to local state, not raise a 500."""
        table = SharedTable()

        async def _boom():
            raise RuntimeError("dynamodb unavailable")

        table.load_projects = _boom
        instance = _admin(table, projects={"local": Project(project_id="local", name="Local")})

        listed = asyncio.run(instance._all_projects())
        assert "local" in listed


class TestARevocationReachesTheOtherInstances:
    """A revoked key kept working elsewhere for up to CACHE_TTL_SECONDS.

    `revoke_key` cleared the cache on the instance that served the request — the
    one instance that needed no help. `invalidate_cache()` existed for exactly
    this and nothing ever called it.
    """

    @staticmethod
    def _two_instances(table: SharedTable):
        return APIKeyService(persistence=table), APIKeyService(persistence=table)

    def test_the_other_instance_stops_accepting_it(self):
        table = SharedTable()
        issuer, other = self._two_instances(table)

        _, raw = asyncio.run(issuer.issue_key("p1", "k", ["chat"], "admin"))

        # The other instance validates it once, which caches it locally and
        # establishes its epoch baseline — the state the bug lived in.
        assert asyncio.run(other.validate_key(raw)) is not None

        key_id = next(iter(table.keys))
        assert asyncio.run(issuer.revoke_key(key_id)) is True

        # Force the poll interval to have elapsed rather than sleeping.
        other._revocation_checked_at = 0.0

        assert asyncio.run(other.validate_key(raw)) is None, (
            "a key revoked on another instance was still accepted here — the "
            "revocation never left the instance that served it"
        )

    def test_revoking_bumps_the_shared_signal(self):
        table = SharedTable()
        issuer, _ = self._two_instances(table)
        _, _raw = asyncio.run(issuer.issue_key("p1", "k", ["chat"], "admin"))
        before = table.epoch

        asyncio.run(issuer.revoke_key(next(iter(table.keys))))

        assert table.epoch > before, "revocation left no signal for other instances"

    def test_an_unrelated_key_survives_the_cache_drop(self):
        """Clearing the cache must cost a re-read, not a false rejection.

        The signal is one counter rather than a per-key tombstone, so a
        revocation invalidates cache entries for keys that were not revoked.
        Those keys must come back valid from the table.
        """
        table = SharedTable()
        issuer, other = self._two_instances(table)

        doomed_key, doomed_raw = asyncio.run(issuer.issue_key("p1", "doomed", ["chat"], "admin"))
        _, kept_raw = asyncio.run(issuer.issue_key("p1", "kept", ["chat"], "admin"))

        assert asyncio.run(other.validate_key(kept_raw)) is not None
        asyncio.run(issuer.revoke_key(doomed_key.key_id))
        other._revocation_checked_at = 0.0

        assert asyncio.run(other.validate_key(kept_raw)) is not None, "cache drop rejected a valid key"
        other._revocation_checked_at = 0.0
        assert asyncio.run(other.validate_key(doomed_raw)) is None

    def test_the_first_check_does_not_clear_a_cache_it_just_built(self):
        """An instance starting against a non-zero epoch must adopt it.

        Treating "first read" as "changed" would make every instance clear its
        cache on its first validation after any revocation had ever happened,
        for the life of the table.
        """
        table = SharedTable()
        table.epoch = 17  # revocations happened long before this instance started
        issuer, late = self._two_instances(table)
        _, raw = asyncio.run(issuer.issue_key("p1", "k", ["chat"], "admin"))

        # Warm the cache and let the instance take its baseline, then re-arm the
        # poll. Order matters: `_check_revocations` runs *before* the cache is
        # read, so on a cold instance the cache is empty at that moment and
        # clearing it is a no-op — a test that validates only once cannot tell a
        # correct first-read from one that wrongly clears. Two validations with a
        # forced re-poll is what makes the assertion below able to fail.
        assert asyncio.run(late.validate_key(raw)) is not None
        assert late._revocation_epoch == 17, "did not adopt the existing epoch as its baseline"
        assert late._cache, "expected a warm cache after a successful validation"

        late._revocation_epoch = None  # as if this were still its first read
        late._revocation_checked_at = 0.0
        reads_before = table.key_reads
        asyncio.run(late.validate_key(raw))

        # Counted as a table read rather than as cache size, for the same reason
        # as the failed-read test: validate_key refills the entry it dropped, so
        # a cleared cache and a preserved one both end up holding one key. Only
        # the extra read distinguishes them.
        assert table.key_reads == reads_before, (
            "cleared a warm cache on what it treated as a first epoch read — every "
            "instance would do this on its first validation for the life of the table"
        )

    def test_the_epoch_is_not_read_on_every_request(self):
        """The cache exists to keep the table off the hot path.

        Polling per request would give that back — so within the poll interval,
        repeated validations must not re-read the epoch.
        """
        table = SharedTable()
        service = APIKeyService(persistence=table)
        _, raw = asyncio.run(service.issue_key("p1", "k", ["chat"], "admin"))

        reads = []
        original = table.get_revocation_epoch

        async def _counted():
            reads.append(1)
            return await original()

        table.get_revocation_epoch = _counted

        for _ in range(5):
            asyncio.run(service.validate_key(raw))

        assert len(reads) == 1, f"read the epoch {len(reads)} times for 5 validations"

    def test_a_failed_epoch_read_falls_back_to_the_ttl(self):
        """An unreadable epoch must not clear the cache on every request.

        Degrading to CACHE_TTL_SECONDS is the documented fallback; degrading to
        "no cache at all" would put every request on DynamoDB during exactly the
        incident where that hurts most.
        """
        table = SharedTable()
        service = APIKeyService(persistence=table)
        _, raw = asyncio.run(service.issue_key("p1", "k", ["chat"], "admin"))
        assert asyncio.run(service.validate_key(raw)) is not None
        cached_before = dict(service._cache)

        async def _fails():
            return None

        table.get_revocation_epoch = _fails
        service._revocation_checked_at = 0.0
        reads_before = table.key_reads
        assert asyncio.run(service.validate_key(raw)) is not None

        # Asserted as "did not re-read the table", not as "the cache still has
        # the same keys": validate_key repopulates the entry it just dropped, so
        # comparing cache keys cannot distinguish a preserved cache from a
        # cleared-and-refilled one. The table read is the observable difference.
        assert table.key_reads == reads_before, "a failed epoch read cleared the cache"
        assert service._cache.keys() == cached_before.keys()

    def test_in_memory_mode_does_not_poll(self):
        """Persistence off means one process, so there is nobody to hear it."""
        table = SharedTable()
        table.enabled = False
        service = APIKeyService(persistence=table)
        _, raw = asyncio.run(service.issue_key("p1", "k", ["chat"], "admin"))

        called = []
        async def _seen():
            called.append(1)
            return 0
        table.get_revocation_epoch = _seen

        asyncio.run(service.validate_key(raw))
        assert called == [], "polled the revocation epoch with persistence disabled"
