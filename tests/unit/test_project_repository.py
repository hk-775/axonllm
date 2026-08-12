"""Authoritative tenant-qualified project repository coverage."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.gateway.auth.project_repository import (
    DynamoProjectRepository,
    ProjectConfigConflict,
    ProjectStoreUnavailable,
)
from src.gateway.models import Project
from src.gateway.persistence import (
    DynamoPersistence,
    PersistenceConflictError,
)


class _Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []
        self.fail_reads = False

    def get_item(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("read unavailable")
        self.calls.append(deepcopy(kwargs))
        key = kwargs["Key"]
        item = self.items.get((key["PK"], key["SK"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def scan(self, **kwargs):
        return {"Items": [deepcopy(item) for item in self.items.values()]}


class _Persistence(DynamoPersistence):
    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__()
        self._enabled = enabled
        self.table = _Table()
        self.save_calls: list[tuple[Project, int | None]] = []
        self.save_error: Exception | None = None

    def _get_table(self) -> _Table:
        return self.table

    async def save_project(
        self,
        project: Project,
        *,
        expected_revision: int | None = None,
    ) -> int:
        self.save_calls.append((deepcopy(project), expected_revision))
        if self.save_error is not None:
            raise self.save_error
        assert expected_revision is not None
        return expected_revision + 1


def _store(persistence: _Persistence, project: Project) -> None:
    item = persistence.serialize_project(project)
    persistence.table.items[(item["PK"], item["SK"])] = item


async def test_same_project_id_is_isolated_by_tenant() -> None:
    persistence = _Persistence()
    repository = DynamoProjectRepository(persistence)
    tenant_a = Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Tenant A",
    )
    tenant_b = Project(
        project_id="shared",
        tenant_id="tenant-b",
        name="Tenant B",
    )
    _store(persistence, tenant_a)
    _store(persistence, tenant_b)

    assert await repository.resolve("tenant-a", "shared") == tenant_a
    assert await repository.resolve("tenant-b", "shared") == tenant_b
    assert persistence.table.calls[-1]["ConsistentRead"] is True


async def test_legacy_project_loader_excludes_tenant_owned_rows() -> None:
    persistence = _Persistence()
    legacy = Project(project_id="shared", name="Legacy")
    _store(persistence, legacy)
    _store(
        persistence,
        Project(
            project_id="shared",
            tenant_id="tenant-a",
            name="Tenant A",
        ),
    )
    _store(
        persistence,
        Project(
            project_id="shared",
            tenant_id="tenant-b",
            name="Tenant B",
        ),
    )

    assert await persistence.load_projects() == {"shared": legacy}


async def test_legacy_global_project_is_not_canonical_ownership() -> None:
    persistence = _Persistence()
    repository = DynamoProjectRepository(persistence)
    _store(persistence, Project(project_id="shared", name="Legacy"))

    assert await repository.resolve("tenant-a", "shared") is None


async def test_missing_project_returns_none() -> None:
    repository = DynamoProjectRepository(_Persistence())

    assert await repository.resolve("tenant-a", "missing") is None


async def test_disabled_or_unavailable_store_fails_closed() -> None:
    disabled = DynamoProjectRepository(_Persistence(enabled=False))
    with pytest.raises(ProjectStoreUnavailable):
        await disabled.resolve("tenant-a", "shared")

    persistence = _Persistence()
    persistence.table.fail_reads = True
    with pytest.raises(ProjectStoreUnavailable):
        await DynamoProjectRepository(persistence).resolve(
            "tenant-a",
            "shared",
        )


async def test_malformed_owner_row_fails_closed() -> None:
    persistence = _Persistence()
    project = Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Tenant A",
    )
    item = persistence.serialize_project(project)
    item["tenant_id"] = "tenant-b"
    persistence.table.items[(item["PK"], item["SK"])] = item

    with pytest.raises(ProjectStoreUnavailable):
        await DynamoProjectRepository(persistence).resolve(
            "tenant-a",
            "shared",
        )


async def test_conditional_update_returns_only_the_committed_revision() -> None:
    persistence = _Persistence()
    repository = DynamoProjectRepository(persistence)
    project = Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Updated",
        revision=7,
    )

    committed = await repository.update(
        project,
        expected_revision=7,
    )

    assert committed == Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Updated",
        revision=8,
        created_at=project.created_at,
    )
    assert project.revision == 7
    assert persistence.save_calls == [(project, 7)]


async def test_conditional_update_maps_conflict_and_store_failure() -> None:
    project = Project(
        project_id="shared",
        tenant_id="tenant-a",
        name="Updated",
        revision=7,
    )
    conflict = _Persistence()
    conflict.save_error = PersistenceConflictError("stale")
    with pytest.raises(ProjectConfigConflict):
        await DynamoProjectRepository(conflict).update(
            project,
            expected_revision=7,
        )

    unavailable = _Persistence()
    unavailable.save_error = RuntimeError("dynamodb unavailable")
    with pytest.raises(ProjectStoreUnavailable):
        await DynamoProjectRepository(unavailable).update(
            project,
            expected_revision=7,
        )


async def test_conditional_update_requires_tenant_owned_project() -> None:
    repository = DynamoProjectRepository(_Persistence())

    with pytest.raises(ProjectStoreUnavailable):
        await repository.update(
            Project(project_id="legacy", name="Legacy"),
            expected_revision=0,
        )
