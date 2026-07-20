"""Regression tests for task #4 — persistence table config must match the CDK.

The bug: persistence.py defaulted to a table name ("llm-router-state", PK/SK)
that the CDK never provisioned (it made three differently-named tables with
lowercase pk/sk), and nothing passed a table name to the code. Every write hit
AccessDenied/ResourceNotFound and was silently swallowed.

These tests pin the contract that keeps app and infra in sync:
- the table name is read from AXON_DYNAMODB_TABLE
- the fallback default matches the CDK's provisioned table name (axonllm-state)
- health_status() reports config without touching AWS when disabled
"""

from __future__ import annotations

import asyncio

import pytest

from src.gateway.persistence import DynamoPersistence

# Must equal infra/stack.py -> dynamodb.Table(table_name=...). If either side
# changes, this test fails and forces the other to be updated in lockstep.
CDK_TABLE_NAME = "axonllm-state"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AXON_DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)


class TestTableNameResolution:
    def test_default_matches_cdk_table_name(self):
        assert DynamoPersistence(region="us-east-1")._table_name == CDK_TABLE_NAME

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("AXON_DYNAMODB_TABLE", "custom-table")
        assert DynamoPersistence(region="us-east-1")._table_name == "custom-table"

    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("AXON_DYNAMODB_TABLE", "env-table")
        assert DynamoPersistence(table_name="arg-table")._table_name == "arg-table"


class TestHealthStatus:
    def test_disabled_reports_without_touching_aws(self):
        p = DynamoPersistence(region="us-east-1")  # disabled by default
        status = asyncio.run(p.health_status())
        assert status["enabled"] is False
        assert status["reachable"] is None  # no AWS call attempted
        assert status["table"] == CDK_TABLE_NAME
        assert status["last_write_error"] is None

    def test_write_failure_is_recorded(self):
        p = DynamoPersistence(region="us-east-1")
        assert p.last_write_error is None
        p._record_write_failure("usage record", "req-123")
        assert p.last_write_error == "usage record req-123"
