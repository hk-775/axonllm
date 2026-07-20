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
