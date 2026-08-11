"""Strict payload parsing for the AgentCore invocation boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.gateway.request_validator import RequestValidator

from .errors import AgentCoreAdapterError


class InvocationAction(Enum):
    CHAT = "chat"
    LIST_MODELS = "list_models"
    QUERY = "query"
    HEALTH = "health"


CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "stream",
        "tools",
        "tool_choice",
    }
)
QUERY_FIELDS = frozenset(
    {
        "datasource_id",
        "sql",
        "max_rows",
        "request_id",
    }
)
AUTHORITY_FIELDS = frozenset(
    {
        "user_id",
        "project_id",
        "tenant",
        "tenant_id",
        "roles",
        "scopes",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SQL_BYTES = 64 * 1024
_MAX_QUERY_ROWS = 10_000
_QUERY_RESPONSE_FIELDS = frozenset(
    {
        "request_id",
        "datasource_id",
        "project_id",
        "query_execution_id",
        "columns",
        "rows",
        "row_count",
        "truncated",
        "statistics",
    }
)
_QUERY_STATISTICS_FIELDS = frozenset(
    {
        "data_scanned_bytes",
        "engine_execution_ms",
        "result_bytes",
    }
)


class QueryResponseValidationError(ValueError):
    """The query service returned a response outside its public contract."""


def _required_string(
    value: Any,
    name: str,
    *,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise _invalid_payload(
            f"Field '{name}' must be a non-empty string without surrounding whitespace or control characters."
        )
    return value


def _identifier(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=128)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise _invalid_payload(f"Field '{name}' contains unsupported identifier characters.")
    return normalized


def _optional_request_id(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, "request_id", max_length=128)


def _optional_max_rows(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_QUERY_ROWS:
        raise _invalid_payload(f"Field 'max_rows' must be an integer between 1 and {_MAX_QUERY_ROWS}.")
    return value


def _query_sql(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise _invalid_payload(
            "Field 'sql' must be a non-empty string without surrounding whitespace or null characters."
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid_payload("Field 'sql' must contain valid Unicode text.") from exc
    if len(encoded) > _MAX_SQL_BYTES:
        raise _invalid_payload("Field 'sql' exceeds 64 KiB.")
    return value


@dataclass(frozen=True)
class QueryInvocationRequest:
    """Validated, non-authoritative fields accepted by the query action."""

    datasource_id: str
    sql: str
    max_rows: int | None = None
    request_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QueryInvocationRequest:
        missing = {"datasource_id", "sql"}.difference(payload)
        if missing:
            raise _invalid_payload("Query payload is missing required fields: " + ", ".join(sorted(missing)) + ".")
        return cls(
            datasource_id=_identifier(
                payload["datasource_id"],
                "datasource_id",
            ),
            sql=_query_sql(payload["sql"]),
            max_rows=_optional_max_rows(payload.get("max_rows")),
            request_id=_optional_request_id(payload.get("request_id")),
        )


@dataclass(frozen=True)
class QueryColumn:
    """One validated query result column."""

    name: str
    athena_type: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.athena_type}


@dataclass(frozen=True)
class QueryStatistics:
    """Non-negative Athena execution statistics."""

    data_scanned_bytes: int
    engine_execution_ms: int
    result_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "data_scanned_bytes": self.data_scanned_bytes,
            "engine_execution_ms": self.engine_execution_ms,
            "result_bytes": self.result_bytes,
        }


def _response_object(
    value: Any,
    name: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise QueryResponseValidationError(f"{name} must be an object")
    if set(value) != fields:
        raise QueryResponseValidationError(f"{name} fields do not match the response contract")
    return value


def _response_string(
    value: Any,
    name: str,
    *,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise QueryResponseValidationError(f"{name} is invalid")
    return value


def _response_identifier(value: Any, name: str) -> str:
    normalized = _response_string(value, name, max_length=128)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise QueryResponseValidationError(f"{name} is invalid")
    return normalized


def _column_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SQL_BYTES:
        raise QueryResponseValidationError(f"{name} is invalid")
    return value


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryResponseValidationError(f"{name} must be a non-negative integer")
    return value


def _query_columns(value: Any) -> tuple[QueryColumn, ...]:
    if type(value) is not list:
        raise QueryResponseValidationError("columns must be an array")
    columns: list[QueryColumn] = []
    for raw_column in value:
        column = _response_object(
            raw_column,
            "column",
            frozenset({"name", "type"}),
        )
        columns.append(
            QueryColumn(
                name=_column_text(
                    column["name"],
                    "column name",
                ),
                athena_type=_column_text(
                    column["type"],
                    "column type",
                ),
            )
        )
    return tuple(columns)


def _query_rows(
    value: Any,
    *,
    column_count: int,
) -> tuple[tuple[str | None, ...], ...]:
    if type(value) is not list or len(value) > _MAX_QUERY_ROWS:
        raise QueryResponseValidationError("rows must be a bounded array")
    rows: list[tuple[str | None, ...]] = []
    for raw_row in value:
        if type(raw_row) is not list or len(raw_row) != column_count:
            raise QueryResponseValidationError("query result row width does not match columns")
        if any(item is not None and not isinstance(item, str) for item in raw_row):
            raise QueryResponseValidationError("query result values must be strings or null")
        rows.append(tuple(raw_row))
    return tuple(rows)


@dataclass(frozen=True)
class QueryInvocationResponse:
    """Validated AgentCore representation of a query service result."""

    request_id: str
    datasource_id: str
    project_id: str
    query_execution_id: str
    columns: tuple[QueryColumn, ...]
    rows: tuple[tuple[str | None, ...], ...]
    row_count: int
    truncated: bool
    statistics: QueryStatistics

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        expected_datasource_id: str,
        expected_project_id: str,
        expected_request_id: str | None = None,
    ) -> QueryInvocationResponse:
        value = _response_object(
            raw,
            "query response",
            _QUERY_RESPONSE_FIELDS,
        )
        request_id = _response_string(
            value["request_id"],
            "request_id",
            max_length=128,
        )
        datasource_id = _response_identifier(
            value["datasource_id"],
            "datasource_id",
        )
        project_id = _response_identifier(
            value["project_id"],
            "project_id",
        )
        if (
            datasource_id != expected_datasource_id
            or project_id != expected_project_id
            or (expected_request_id is not None and request_id != expected_request_id)
        ):
            raise QueryResponseValidationError("query response identity does not match the request")

        columns = _query_columns(value["columns"])
        rows = _query_rows(
            value["rows"],
            column_count=len(columns),
        )
        row_count = _non_negative_integer(
            value["row_count"],
            "row_count",
        )
        if row_count != len(rows):
            raise QueryResponseValidationError("row_count does not match query result rows")
        if not isinstance(value["truncated"], bool):
            raise QueryResponseValidationError("truncated must be a boolean")

        raw_statistics = _response_object(
            value["statistics"],
            "statistics",
            _QUERY_STATISTICS_FIELDS,
        )
        statistics = QueryStatistics(
            data_scanned_bytes=_non_negative_integer(
                raw_statistics["data_scanned_bytes"],
                "data_scanned_bytes",
            ),
            engine_execution_ms=_non_negative_integer(
                raw_statistics["engine_execution_ms"],
                "engine_execution_ms",
            ),
            result_bytes=_non_negative_integer(
                raw_statistics["result_bytes"],
                "result_bytes",
            ),
        )
        return cls(
            request_id=request_id,
            datasource_id=datasource_id,
            project_id=project_id,
            query_execution_id=_response_string(
                value["query_execution_id"],
                "query_execution_id",
                max_length=256,
            ),
            columns=columns,
            rows=rows,
            row_count=row_count,
            truncated=value["truncated"],
            statistics=statistics,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "datasource_id": self.datasource_id,
            "project_id": self.project_id,
            "query_execution_id": self.query_execution_id,
            "columns": [column.to_dict() for column in self.columns],
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "statistics": self.statistics.to_dict(),
        }


@dataclass(frozen=True)
class ParsedInvocation:
    action: InvocationAction
    request_data: dict[str, Any] | None = None
    query_request: QueryInvocationRequest | None = None


def _invalid_payload(message: str) -> AgentCoreAdapterError:
    return AgentCoreAdapterError(400, "invalid_payload", message)


def parse_invocation_payload(
    payload: Any,
    *,
    validator: RequestValidator | None = None,
) -> ParsedInvocation:
    """Validate an action-specific JSON object without coercing field types."""
    if type(payload) is not dict:
        raise _invalid_payload("Invocation payload must be a JSON object.")
    if any(not isinstance(key, str) for key in payload):
        raise _invalid_payload("Invocation payload keys must be strings.")

    supplied_authority = sorted(AUTHORITY_FIELDS.intersection(payload))
    if supplied_authority:
        raise AgentCoreAdapterError(
            400,
            "untrusted_identity_fields",
            "Identity and authorization fields are not accepted in payloads.",
        )

    raw_action = payload.get("action", InvocationAction.CHAT.value)
    if not isinstance(raw_action, str):
        raise _invalid_payload("Field 'action' must be a string.")
    try:
        action = InvocationAction(raw_action)
    except ValueError as exc:
        raise _invalid_payload("Field 'action' is not supported.") from exc

    allowed_fields = {"action"}
    if action is InvocationAction.CHAT:
        allowed_fields.update(CHAT_FIELDS)
    elif action is InvocationAction.QUERY:
        allowed_fields.update(QUERY_FIELDS)
    unexpected = sorted(set(payload).difference(allowed_fields))
    if unexpected:
        raise _invalid_payload("Invocation payload contains unsupported fields: " + ", ".join(unexpected) + ".")

    if action is InvocationAction.QUERY:
        return ParsedInvocation(
            action=action,
            query_request=QueryInvocationRequest.from_payload(payload),
        )
    if action is not InvocationAction.CHAT:
        return ParsedInvocation(action=action)

    request_data = {field_name: payload[field_name] for field_name in CHAT_FIELDS if field_name in payload}
    request_data.setdefault("stream", False)
    errors = (validator or RequestValidator()).validate_payload(
        request_data,
        allow_empty_model=False,
        check_model=False,
    )
    if errors:
        raise _invalid_payload(errors[0].message)
    return ParsedInvocation(
        action=action,
        request_data=request_data,
    )
