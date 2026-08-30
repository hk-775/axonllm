"""Credential-safety contracts for the seeded demo personas."""

from __future__ import annotations

import json
import stat

import pytest

from scripts.bootstrap_demo_access import (
    PERSONA_SCHEMA,
    _personas_from_seed,
    _store_local_file,
)


def test_shipped_seed_produces_one_persona_per_tenant() -> None:
    personas = _personas_from_seed("config/demo_seed_multitenant.yaml")

    assert {
        (persona.tenant_id, persona.project_id)
        for persona in personas
    } == {
        ("tenant-acme", "proj-alpha"),
        ("tenant-globex", "proj-alpha"),
    }


def test_duplicate_tenant_personas_are_rejected(tmp_path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        """
tenants:
  - tenant_id: tenant-a
    admin_project_id: project-a
    persona_label: First
  - tenant_id: tenant-a
    admin_project_id: project-b
    persona_label: Duplicate
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate demo tenant"):
        _personas_from_seed(str(seed))


def test_local_persona_document_is_private_and_complete(tmp_path) -> None:
    target = tmp_path / "access" / "personas.json"
    document = {
        "schema": PERSONA_SCHEMA,
        "personas": [
            {
                "tenant_id": "tenant-a",
                "project_id": "project-a",
                "label": "Tenant A administrator",
                "api_key": "one-time-value",
            }
        ],
    }

    location = _store_local_file(target, document=document)

    assert location == str(target.resolve())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == document
    assert list(target.parent.glob(f".{target.name}.*")) == []
