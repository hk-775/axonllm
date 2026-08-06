"""Tests for the `axon issue-key` bootstrap command (task #11).

Solves the chicken-and-egg where minting the first key over HTTP requires an
admin key that doesn't exist yet. The CLI mints in-process via APIKeyService.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.persistence import DynamoPersistence


class TestPackaging:
    def test_package_imports_as_src_gateway(self):
        # The whole codebase imports `from src.gateway...`; the package must be
        # importable under that exact path (regression for the pyproject fix).
        import src.gateway.cli  # noqa: F401
        import src.gateway.bootstrap  # noqa: F401
        import src.gateway.agent  # noqa: F401

    def test_cli_exposes_issue_key(self):
        from src.gateway import cli

        assert hasattr(cli, "cmd_issue_key")
        assert hasattr(cli, "main")


class TestIssueKey:
    def test_mints_axon_prefixed_key(self):
        # In-process mint (persistence disabled → not saved, but a valid key is
        # generated and returned). Mirrors what cmd_issue_key does.
        persistence = DynamoPersistence(region="us-east-1")  # disabled by default
        service = APIKeyService(persistence=persistence)

        async def _issue():
            return await service.issue_key(
                project_id="demo", name="test", scopes=["chat"], created_by="cli",
            )

        record, raw_key = asyncio.run(_issue())
        assert raw_key.startswith("axon_")
        assert record.project_id == "demo"
        assert record.scopes == ["chat"]
        assert record.created_by == "cli"

    def test_issued_key_validates(self):
        # A freshly issued key validates against the same service (cache path).
        persistence = DynamoPersistence(region="us-east-1")
        service = APIKeyService(persistence=persistence)

        async def _roundtrip():
            _, raw = await service.issue_key(
                project_id="demo", name="t", scopes=["chat"], created_by="cli",
            )
            return await service.validate_key(raw)

        validated = asyncio.run(_roundtrip())
        assert validated is not None
        assert validated.project_id == "demo"

    def test_cmd_issue_key_prints_key(self, capsys, monkeypatch):
        # Drive the actual CLI handler with a fake args namespace.
        from src.gateway import cli

        monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
        args = SimpleNamespace(project="p1", name="n1", scopes="chat,admin")
        cli.cmd_issue_key(args)
        out = capsys.readouterr().out
        assert "axon_" in out
        assert "p1" in out


class TestMissingProjectNote:
    """A key minted for a project that does not exist should say so.

    `issue-key` deliberately does not create the project — a project id scopes a
    key rather than referencing a record, and requiring the record would restore
    the bootstrap deadlock this command exists to break. But the silence was its
    own trap: the key works, chat succeeds, spend accrues, and the project is
    absent from /admin/projects with no budget_limit, so nothing is capped and
    nothing says why. Observed live before this note existed.
    """

    @staticmethod
    def _run(monkeypatch, *, enabled, project=None):
        """Drive the real handler with persistence stubbed at the class level."""
        from src.gateway import cli
        from src.gateway.persistence import DynamoPersistence

        monkeypatch.setattr(DynamoPersistence, "enabled", property(lambda self: enabled))

        async def _get_project(self, project_id):
            return project

        monkeypatch.setattr(DynamoPersistence, "get_project", _get_project)

        async def _noop(self):
            return None

        monkeypatch.setattr(DynamoPersistence, "create_table_if_not_exists", _noop)
        cli.cmd_issue_key(SimpleNamespace(project="ghost", name="n", scopes="chat"))

    def test_notes_the_absence_and_shows_how_to_fix_it(self, capsys, monkeypatch):
        self._run(monkeypatch, enabled=True, project=None)
        captured = capsys.readouterr()
        # The key is still printed: the note is advisory, not a refusal.
        assert "axon_" in captured.out
        assert "no project 'ghost' was found" in captured.err
        assert "budget" in captured.err
        # Must be actionable, not just a complaint.
        assert "/admin/projects" in captured.err

    def test_stays_quiet_when_the_project_exists(self, capsys, monkeypatch):
        self._run(monkeypatch, enabled=True, project=object())
        captured = capsys.readouterr()
        assert "axon_" in captured.out
        assert "no project" not in captured.err

    def test_defers_to_the_persistence_warning_when_disabled(self, capsys, monkeypatch):
        """With persistence off, every project reads as missing — the absence is
        meaningless and the existing "NOT persisted" warning is the real problem.
        Printing both would bury it under a note about a project that could not
        have been read in the first place."""
        self._run(monkeypatch, enabled=False)
        captured = capsys.readouterr()
        assert "NOT persisted" in captured.err
        assert "no project" not in captured.err


class TestFailureHint:
    """A failed CLI request should name the actual cause.

    Every exception used to print "Is the server running? Try: axon serve" —
    including 401/403, which prove the server *is* running and only the
    credential is absent. And the bare `axon` in the hint does not resolve after
    `uv sync`, since the console script lands in `.venv/bin` and that is not on
    PATH.
    """

    def test_auth_codes_say_the_server_is_up(self):
        from urllib.error import HTTPError

        from src.gateway import cli

        for status in (401, 403):
            err = HTTPError("http://localhost:8000/api/models", status, "denied", {}, None)
            hint = cli._failure_hint(err, 8000)
            assert "is running" in hint
            assert "AXON_API_KEY" in hint
            assert "Is the server running" not in hint

    def test_connection_failure_still_suggests_starting_it(self):
        from src.gateway import cli

        hint = cli._failure_hint(OSError("Connection refused"), 8123)
        assert "8123" in hint
        assert "Is the server running" in hint

    def test_hints_never_suggest_a_bare_axon(self):
        from urllib.error import HTTPError

        from src.gateway import cli

        hints = [
            cli._failure_hint(OSError("Connection refused"), 8000),
            cli._failure_hint(
                HTTPError("http://localhost:8000/api/models", 401, "denied", {}, None), 8000
            ),
        ]
        for hint in hints:
            # `axon` may appear, but only ever as `uv run axon`.
            for idx in range(len(hint)):
                if hint.startswith("axon", idx):
                    assert hint[max(0, idx - 7):idx] == "uv run "
