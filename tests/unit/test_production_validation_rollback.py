from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import production_validation_rollback as rollback


def _journal(path: Path) -> rollback.RollbackJournal:
    return rollback.RollbackJournal.create(
        path,
        clock=lambda: "2026-08-12T12:00:00+00:00",
    )


def _prepare(
    journal: rollback.RollbackJournal,
    credential_type: str,
) -> str:
    return journal.prepare(
        endpoint="https://control.example.com",
        path="/admin/projects/project-a",
        credential_env="AXON_ADMIN_SESSION",
        credential_type=credential_type,
        csrf_token_env="AXON_ADMIN_CSRF",
        timeout_seconds=5,
        prior_revision=7,
        prior_values={"cache_enabled": False},
        mutation_values={"cache_enabled": True},
    )


@pytest.mark.parametrize(
    "credential_type",
    ["alb-session-cookie", "browser-session-cookie"],
)
def test_journal_accepts_each_control_plane_cookie_mode(
    tmp_path: Path,
    credential_type: str,
) -> None:
    path = tmp_path / f"{credential_type}.json"
    journal = _journal(path)
    entry_id = _prepare(journal, credential_type)

    reopened = rollback.RollbackJournal.open(
        path,
        clock=lambda: "2026-08-12T12:00:01+00:00",
    )
    entry = reopened.entries()[0]
    assert entry["id"] == entry_id
    assert entry["credentialType"] == credential_type


def test_credential_type_is_part_of_entry_identity(
    tmp_path: Path,
) -> None:
    alb_id = _prepare(
        _journal(tmp_path / "alb.json"),
        "alb-session-cookie",
    )
    browser_id = _prepare(
        _journal(tmp_path / "browser.json"),
        "browser-session-cookie",
    )

    assert alb_id != browser_id


def test_journal_rejects_non_browser_credentials(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "invalid.json")

    with pytest.raises(
        rollback.RollbackJournalError,
        match="entry is invalid",
    ):
        _prepare(journal, "bearer")

    assert journal.entries() == ()
