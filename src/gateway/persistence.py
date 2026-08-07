"""DynamoDB persistence layer for LLM Router state.

Controlled by LLM_ROUTER_DYNAMODB_ENABLED env var (default: "false").
Uses a single DynamoDB table with composite keys (PK/SK pattern).
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal

from src.gateway.models import (
    APIKey,
    FeedbackRecord,
    GuardrailRule,
    PolicyNode,
    Project,
    ScimGroup,
    ScimUser,
    UsageRecord,
)

logger = logging.getLogger(__name__)


def tenant_project_partition_key(tenant_id: str) -> str:
    """DynamoDB partition key for one tenant's project namespace."""
    return f"TENANT#{tenant_id}"


def tenant_project_sort_key(project_id: str) -> str:
    """DynamoDB sort key for a project inside a tenant namespace."""
    return f"PROJECT#{project_id}"


class DynamoPersistence:
    """DynamoDB persistence layer for LLM Router state."""

    def __init__(
        self,
        table_name: str | None = None,
        region: str = "us-east-1",
    ):
        # Table name comes from AXON_DYNAMODB_TABLE so the app and the CDK stack
        # agree on a single source of truth. The CDK provisions a table by this
        # exact name (see infra/stack.py) and passes it in via the env var; the
        # "axonllm-state" fallback matches the stack default for local/manual runs.
        self._table_name = (
            table_name
            or os.environ.get("AXON_DYNAMODB_TABLE", "axonllm-state")
        )
        self._region = region
        self._enabled = os.environ.get(
            "LLM_ROUTER_DYNAMODB_ENABLED", "false"
        ).lower() == "true"
        self._table = None
        self._dynamodb = None
        self._init_lock = threading.Lock()
        # Set to a short reason string when a write is dropped, so a health probe
        # can surface silent persistence failures instead of losing data quietly.
        self.last_write_error: str | None = None
        # Whether the last usage-record scan raised. Lets
        # ``load_usage_records_or_none`` distinguish an outage from an empty table
        # without changing ``load_usage_records``' return-[] contract.
        self._last_scan_failed = False
        # Same idea for the Cedar policy scan, where mistaking an outage for an
        # empty set would drop every enforced policy rather than lose a count.
        self._last_policy_scan_failed = False
        # And for the two config scans a live refresh re-reads. Same stakes as
        # the policy one: adopting an empty result would un-enforce every budget
        # and model restriction the fleet is running, so the refresh needs to
        # tell "the table is empty" from "the scan failed".
        self._last_project_scan_failed = False
        self._last_user_config_scan_failed = False

    @property
    def enabled(self) -> bool:
        """Whether DynamoDB persistence is active."""
        return self._enabled

    def _record_write_failure(self, what: str, ident: str) -> None:
        """Log a dropped write at ERROR and remember it for the health probe.

        A silently-swallowed write means billing/usage/config data is lost with
        no signal — the failure mode that made persistence dead-on-arrival. We
        still don't raise (a provider call shouldn't 500 because Dynamo hiccuped),
        but we log loudly and expose it via health_status() so ops can detect it.
        """
        self.last_write_error = f"{what} {ident}"
        logger.error("Failed to persist %s %s to DynamoDB", what, ident, exc_info=True)

    async def health_status(self) -> dict:
        """Report persistence reachability for a health probe.

        Returns {"enabled": bool, "reachable": bool, "table": str,
        "last_write_error": str | None}. When enabled, performs a cheap
        describe_table so a misconfigured/missing table or IAM denial surfaces
        instead of silently dropping every write.
        """
        status = {
            "enabled": self._enabled,
            "table": self._table_name,
            "last_write_error": self.last_write_error,
            "reachable": None,
        }
        if not self._enabled:
            return status

        def _describe() -> bool:
            import boto3

            if self._dynamodb is None:
                self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
            self._dynamodb.meta.client.describe_table(TableName=self._table_name)
            return True

        try:
            status["reachable"] = await asyncio.to_thread(_describe)
        except Exception as exc:
            status["reachable"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "DynamoDB table %s not reachable: %s", self._table_name, exc, exc_info=True
            )
        return status

    def _get_table(self):
        """Lazily create the boto3 DynamoDB Table resource (thread-safe)."""
        if self._table is None:
            with self._init_lock:
                if self._table is None:
                    import boto3

                    self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
                    self._table = self._dynamodb.Table(self._table_name)
        return self._table

    # --- Table management ---

    async def create_table_if_not_exists(self) -> None:
        """Create the DynamoDB table if it doesn't already exist."""
        if not self._enabled:
            return

        def _create():
            import boto3

            if self._dynamodb is None:
                self._dynamodb = boto3.resource(
                    "dynamodb", region_name=self._region
                )

            client = self._dynamodb.meta.client
            try:
                client.describe_table(TableName=self._table_name)
            except client.exceptions.ResourceNotFoundException:
                self._dynamodb.create_table(
                    TableName=self._table_name,
                    KeySchema=[
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "PK", "AttributeType": "S"},
                        {"AttributeName": "SK", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                # Wait until the table exists
                client.get_waiter("table_exists").wait(
                    TableName=self._table_name
                )
                logger.info("Created DynamoDB table %s", self._table_name)
            # Reset cached table reference so it picks up the new table
            self._table = None

        try:
            await asyncio.to_thread(_create)
        except Exception:
            logger.warning(
                "Failed to create DynamoDB table %s", self._table_name, exc_info=True
            )

    # --- UsageRecord serialization ---

    @staticmethod
    def serialize_usage_record(record: UsageRecord) -> dict:
        """Serialize a UsageRecord to a DynamoDB item dict."""
        ts_iso = record.timestamp.isoformat()
        return {
            "PK": f"USAGE#{record.request_id}",
            "SK": f"USAGE#{ts_iso}",
            "entity_type": "usage_record",
            "request_id": record.request_id,
            "project_id": record.project_id,
            "user_id": record.user_id,
            "provider": record.provider,
            "model": record.model,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cost": record.cost,
            "timestamp": ts_iso,
            "cached_tokens": record.cached_tokens,
            "cache_creation_tokens": record.cache_creation_tokens,
            "image_tokens": record.image_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "latency_ms": record.latency_ms,
            "status": record.status,
            "routing_strategy": record.routing_strategy,
            "task_type": record.task_type,
            "provider_request_id": record.provider_request_id,
        }

    @staticmethod
    def deserialize_usage_record(item: dict) -> UsageRecord:
        """Deserialize a DynamoDB item dict to a UsageRecord."""
        return UsageRecord(
            request_id=item["request_id"],
            project_id=item["project_id"],
            user_id=item["user_id"],
            provider=item["provider"],
            model=item["model"],
            prompt_tokens=int(item["prompt_tokens"]),
            completion_tokens=int(item["completion_tokens"]),
            total_tokens=int(item["total_tokens"]),
            cost=float(item["cost"]),
            timestamp=datetime.fromisoformat(item["timestamp"]),
            cached_tokens=int(item.get("cached_tokens", 0)),
            cache_creation_tokens=int(item.get("cache_creation_tokens", 0)),
            image_tokens=int(item.get("image_tokens", 0)),
            reasoning_tokens=int(item.get("reasoning_tokens", 0)),
            # float() is not redundant: _convert_decimals_to_native narrows a
            # whole-valued Decimal to int, so a latency that happens to land on
            # 1234.0 comes back as an int and the field's declared type silently
            # stops holding.
            latency_ms=float(item.get("latency_ms", 0.0)),
            # "" for a row written before this field existed, NOT "success" —
            # the dataclass default. Defaulting an unknown row to "success"
            # would manufacture an error rate: every pre-migration record would
            # assert it succeeded, and the one thing a reader wants from this
            # field is which requests failed.
            status=str(item.get("status", "")),
            routing_strategy=str(item.get("routing_strategy", "")),
            # Absent on every row written before this field existed. Defaulting to
            # "" rather than "general" is the whole point: an unclassified record
            # must not be counted as a classification result.
            task_type=str(item.get("task_type", "")),
            provider_request_id=str(item.get("provider_request_id", "")),
        )

    # --- Project serialization ---

    @staticmethod
    def serialize_project(project: Project) -> dict:
        """Serialize a Project to a DynamoDB item dict."""
        guardrail_rules = [
            {
                "name": rule.name,
                "rule_type": rule.rule_type,
                "pattern": rule.pattern,
                "action": rule.action,
                "applies_to": rule.applies_to,
            }
            for rule in project.guardrail_rules
        ]
        if project.tenant_id is None:
            partition_key = f"PROJECT#{project.project_id}"
            sort_key = "PROJECT"
        else:
            partition_key = tenant_project_partition_key(project.tenant_id)
            sort_key = tenant_project_sort_key(project.project_id)

        item = {
            "PK": partition_key,
            "SK": sort_key,
            "entity_type": "project",
            "project_id": project.project_id,
            "name": project.name,
            "budget_limit": project.budget_limit,
            "alert_threshold": project.alert_threshold,
            "allowed_models": json.dumps(project.allowed_models),
            "guardrail_rules": json.dumps(guardrail_rules),
            "cache_enabled": project.cache_enabled,
            "cache_ttl_seconds": project.cache_ttl_seconds,
            "semantic_cache_enabled": project.semantic_cache_enabled,
            "semantic_cache_threshold": project.semantic_cache_threshold,
            "log_level": project.log_level,
            "log_destination": project.log_destination,
            "ltm_enabled": project.ltm_enabled,
            "retention_period_hours": project.retention_period_hours,
            "rate_limit_rpm": project.rate_limit_rpm,
            "members": json.dumps(project.members),
            "created_at": project.created_at.isoformat(),
        }
        if project.tenant_id is not None:
            item["tenant_id"] = project.tenant_id
        return item

    @staticmethod
    def deserialize_project(item: dict) -> Project:
        """Deserialize a DynamoDB item dict to a Project."""
        allowed_models_raw = item.get("allowed_models")
        if isinstance(allowed_models_raw, str):
            allowed_models = json.loads(allowed_models_raw)
        else:
            allowed_models = allowed_models_raw

        guardrail_rules_raw = item.get("guardrail_rules", "[]")
        if isinstance(guardrail_rules_raw, str):
            guardrail_dicts = json.loads(guardrail_rules_raw)
        else:
            guardrail_dicts = guardrail_rules_raw
        guardrail_rules = [
            GuardrailRule(
                name=g["name"],
                rule_type=g["rule_type"],
                pattern=g.get("pattern"),
                action=g["action"],
                applies_to=g["applies_to"],
            )
            for g in guardrail_dicts
        ]

        members_raw = item.get("members", "[]")
        if isinstance(members_raw, str):
            members = json.loads(members_raw)
        else:
            members = members_raw

        return Project(
            project_id=item["project_id"],
            name=item["name"],
            tenant_id=item.get("tenant_id"),
            budget_limit=float(item["budget_limit"]) if item.get("budget_limit") is not None else None,
            alert_threshold=float(item["alert_threshold"]) if item.get("alert_threshold") is not None else None,
            allowed_models=allowed_models,
            guardrail_rules=guardrail_rules,
            cache_enabled=bool(item.get("cache_enabled", False)),
            cache_ttl_seconds=int(item.get("cache_ttl_seconds", 300)),
            semantic_cache_enabled=bool(item.get("semantic_cache_enabled", False)),
            # float(), not the raw value: DynamoDB returns numbers as Decimal,
            # and a Decimal threshold compares fine but would not round-trip
            # through JSON on the admin surface. None stays None — see the note
            # on the field, 0.0 would mean "match everything".
            semantic_cache_threshold=(
                float(item["semantic_cache_threshold"])
                if item.get("semantic_cache_threshold") is not None
                else None
            ),
            log_level=item.get("log_level", "INFO"),
            log_destination=item.get("log_destination"),
            ltm_enabled=bool(item.get("ltm_enabled", False)),
            retention_period_hours=int(item.get("retention_period_hours", 24)),
            rate_limit_rpm=int(item["rate_limit_rpm"]) if item.get("rate_limit_rpm") is not None else None,
            members=members,
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else datetime.now(timezone.utc),
        )

    # --- UserConfig serialization ---

    @staticmethod
    def serialize_user_config(user_id: str, config: dict) -> dict:
        """Serialize a user configuration to a DynamoDB item dict."""
        allowed_models = config.get("allowed_models")
        return {
            "PK": f"USER#{user_id}",
            "SK": "CONFIG",
            "entity_type": "user_config",
            "user_id": user_id,
            "allowed_models": json.dumps(allowed_models) if allowed_models is not None else None,
            "budget_limit": config.get("budget_limit"),
            "alert_threshold": config.get("alert_threshold"),
        }

    @staticmethod
    def deserialize_user_config(item: dict) -> tuple[str, dict]:
        """Deserialize a DynamoDB item dict to a (user_id, config) tuple."""
        user_id = item["user_id"]

        allowed_models_raw = item.get("allowed_models")
        if isinstance(allowed_models_raw, str):
            allowed_models = json.loads(allowed_models_raw)
        else:
            allowed_models = allowed_models_raw

        config = {
            "allowed_models": allowed_models,
            "budget_limit": float(item["budget_limit"]) if item.get("budget_limit") is not None else None,
            "alert_threshold": float(item["alert_threshold"]) if item.get("alert_threshold") is not None else None,
        }
        return user_id, config

    # --- Helpers ---

    @staticmethod
    def _convert_floats_to_decimal(item: dict) -> dict:
        """Convert float values to Decimal for DynamoDB compatibility."""
        converted = {}
        for key, value in item.items():
            if isinstance(value, float):
                converted[key] = Decimal(str(value))
            else:
                converted[key] = value
        return converted

    @staticmethod
    def _convert_decimals_to_native(item: dict) -> dict:
        """Convert Decimal values from DynamoDB back to int/float."""
        converted = {}
        for key, value in item.items():
            if isinstance(value, Decimal):
                if value == int(value):
                    converted[key] = int(value)
                else:
                    converted[key] = float(value)
            else:
                converted[key] = value
        return converted

    # --- Async DynamoDB operations ---

    async def save_usage_record(self, record: UsageRecord) -> None:
        """Serialize and write a UsageRecord to DynamoDB."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_usage_record(record)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("usage record", record.request_id)

    async def load_usage_records_or_none(self) -> list[UsageRecord] | None:
        """Like ``load_usage_records``, but None on failure instead of ``[]``.

        ``load_usage_records`` swallows its exceptions and returns an empty list,
        which is right for startup hydration — a gateway should boot with no
        history rather than refuse to start. It is wrong for a caller that
        rate-limits itself on the result: an outage looks identical to an empty
        store, so the caller records a successful refresh and serves
        single-instance numbers for a full window after the store recovers.

        Kept as a separate method rather than changing the original's contract,
        which several callers depend on.
        """
        if not self._enabled:
            return None
        records = await self.load_usage_records()
        return None if self._last_scan_failed else records

    async def load_usage_records(self) -> list[UsageRecord]:
        """Scan DynamoDB for all usage records and deserialize them."""
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("usage_record")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("usage_record"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            records = []
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                records.append(self.deserialize_usage_record(item))
            self._last_scan_failed = False
            return records
        except Exception:
            logger.warning("Failed to load usage records from DynamoDB", exc_info=True)
            # Recorded rather than raised, so this method's contract (boot with no
            # history rather than refuse to start) is unchanged, while
            # ``load_usage_records_or_none`` can still tell an outage from an
            # empty table.
            self._last_scan_failed = True
            return []

    async def load_audit_records(self, project_id: str | None = None) -> list[dict]:
        """Load persisted audit records (raw dicts), ordered by SK (timestamp).

        Audit rows use PK ``AUDIT#<project_id>`` / SK
        ``AUDIT#<iso>#<record_id>``. Returns them chronologically so the hash
        chain can be reloaded/verified against the durable store.
        """
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            flt = Attr("PK").begins_with("AUDIT#")
            if project_id:
                flt = Attr("PK").eq(f"AUDIT#{project_id}")
            items = []
            response = table.scan(FilterExpression=flt)
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=flt,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            raw = [self._convert_decimals_to_native(i) for i in raw]
            raw.sort(key=lambda i: i.get("SK", ""))
            return raw
        except Exception:
            logger.warning("Failed to load audit records from DynamoDB", exc_info=True)
            return []

    async def get_latest_audit_hash(self) -> str | None:
        """Return the most recent persisted audit record_hash (the chain head).

        Used to reload chain continuity on startup so the hash chain survives
        restarts. None when no audit rows exist.
        """
        rows = await self.load_audit_records()
        return rows[-1].get("record_hash") if rows else None

    async def save_project(self, project: Project) -> None:
        """Serialize and write a Project to DynamoDB."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_project(project)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("project", project.project_id)
            # Don't advertise a change that isn't in the table — see
            # save_cedar_policy for the same reasoning.
            return
        await self.bump_config_version()

    async def get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> Project | None:
        """Read a single project by id.

        A point read rather than a filtered scan: the caller is resolving one
        `{id}` path parameter, and `load_projects()` scans the whole table to
        answer it. Used by `AdminRouter` to resolve a project another instance
        created, which its startup-hydrated dict cannot know about.

        Returns None both for "no such project" and for a read failure — the
        caller renders either as `404`. A transient DynamoDB error therefore
        reads as a missing project, which is the same behaviour the rest of the
        admin API already has for a dropped read, and is why the exception is
        logged rather than swallowed silently.
        """
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            if tenant_id is None:
                key = {"PK": f"PROJECT#{project_id}", "SK": "PROJECT"}
            else:
                key = {
                    "PK": tenant_project_partition_key(tenant_id),
                    "SK": tenant_project_sort_key(project_id),
                }
            resp = table.get_item(Key=key)
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                project = self.deserialize_project(
                    self._convert_decimals_to_native(item)
                )
                if tenant_id is not None and project.tenant_id != tenant_id:
                    logger.error(
                        "Tenant project key returned mismatched owner project=%s "
                        "expected_tenant=%s actual_tenant=%s",
                        project_id,
                        tenant_id,
                        project.tenant_id,
                    )
                    return None
                return project
        except Exception:
            logger.warning(
                "Failed to load project %s for tenant %s from DynamoDB",
                project_id,
                tenant_id,
                exc_info=True,
            )
        return None

    async def load_projects(self) -> dict[str, Project]:
        """Load legacy globally keyed projects for the legacy control plane.

        Canonical tenant-owned projects cannot be represented by this
        ``dict[project_id, Project]`` contract: two tenants may intentionally use
        the same project id. Those rows are resolved only through
        ``get_project(project_id, tenant_id)`` and ``DynamoProjectRepository``.
        Mixing them into this map would let scan order decide which tenant's
        project survives.
        """
        if not self._enabled:
            return {}

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("project")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("project"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            projects = {}
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                project = self.deserialize_project(item)
                if project.tenant_id is not None:
                    continue
                projects[project.project_id] = project
            self._last_project_scan_failed = False
            return projects
        except Exception:
            logger.warning("Failed to load projects from DynamoDB", exc_info=True)
            self._last_project_scan_failed = True
            return {}

    async def save_user_config(self, user_id: str, config: dict) -> None:
        """Serialize and write a user configuration to DynamoDB."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_user_config(user_id, config)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("user config", user_id)
            return
        await self.bump_config_version()

    async def bump_config_version(self) -> int | None:
        """Atomically increment the shared config version, returning the new one.

        One counter covers both projects and user configs rather than one each.
        The refresh re-reads both scans together, so a second counter would only
        let it skip one of two scans it is already making — and two counters can
        disagree about ordering, which one cannot.
        """
        if not self._enabled:
            return None

        def _add():
            resp = self._get_table().update_item(
                Key={"PK": "CONFIG#VERSION", "SK": "TOTAL"},
                UpdateExpression="ADD #v :one",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":one": Decimal("1")},
                ReturnValues="UPDATED_NEW",
            )
            return resp.get("Attributes", {}).get("version")

        try:
            version = await asyncio.to_thread(_add)
            return int(version) if version is not None else None
        except Exception:
            self._record_write_failure("config version", "TOTAL")
            return None

    async def get_config_version(self) -> int | None:
        """Read the shared config version, or None if it could not be read.

        Absent returns 0 for the same reason ``get_policy_version`` does: no
        config has ever been written through the API is a known state, and every
        gateway starts in it. Conflating it with unreadable would mean a
        seed-only deployment never records a successful check and re-reads this
        counter on every request forever.
        """
        if not self._enabled:
            return None

        def _get():
            resp = self._get_table().get_item(
                Key={"PK": "CONFIG#VERSION", "SK": "TOTAL"}
            )
            item = resp.get("Item")
            return item.get("version", 0) if item else 0

        try:
            return int(await asyncio.to_thread(_get))
        except Exception:
            logger.warning("Failed to read the shared config version", exc_info=True)
            return None

    async def load_projects_or_none(self) -> dict[str, Project] | None:
        """Like ``load_projects``, but None on failure instead of ``{}``.

        ``load_projects`` returns ``{}`` on failure so a Dynamo outage cannot
        block startup. On a live refresh that trade inverts: adopting ``{}``
        would drop every project the fleet knows about, and an unresolved project
        means no budget gate and no allowed-models list.
        """
        if not self._enabled:
            return None
        # Reset here rather than relying on the loader to clear it, so this cannot
        # read a flag left set by an earlier caller. That makes the loader's own
        # success-path clear redundant; it is kept because the flag is public
        # enough that a future reader of it should not have to know which of the
        # two resets it depends on.
        self._last_project_scan_failed = False
        projects = await self.load_projects()
        return None if self._last_project_scan_failed else projects

    async def load_user_configs_or_none(self) -> dict[str, dict] | None:
        """Like ``load_user_configs``, but None on failure instead of ``{}``.

        Same reasoning as ``load_projects_or_none``: an adopted empty result
        clears every per-user budget limit and model restriction in the fleet.
        """
        if not self._enabled:
            return None
        self._last_user_config_scan_failed = False
        configs = await self.load_user_configs()
        return None if self._last_user_config_scan_failed else configs

    async def load_user_configs(self) -> dict[str, dict]:
        """Scan DynamoDB for all user configs and deserialize them."""
        if not self._enabled:
            return {}

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("user_config")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("user_config"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            configs = {}
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                user_id, config = self.deserialize_user_config(item)
                configs[user_id] = config
            self._last_user_config_scan_failed = False
            return configs
        except Exception:
            logger.warning("Failed to load user configs from DynamoDB", exc_info=True)
            self._last_user_config_scan_failed = True
            return {}

    # --- FeedbackRecord serialization ---

    @staticmethod
    def serialize_feedback_record(record: FeedbackRecord) -> dict:
        ts_iso = record.timestamp.isoformat()
        return {
            "PK": f"FEEDBACK#{record.request_id}",
            "SK": f"FEEDBACK#{ts_iso}",
            "entity_type": "feedback_record",
            "request_id": record.request_id,
            "timestamp": ts_iso,
            "task_type": record.task_type,
            "confidence": record.confidence,
            "selected_model": record.selected_model,
            "benchmark_score": record.benchmark_score,
        }

    @staticmethod
    def deserialize_feedback_record(item: dict) -> FeedbackRecord:
        return FeedbackRecord(
            request_id=item["request_id"],
            timestamp=datetime.fromisoformat(item["timestamp"]),
            task_type=item["task_type"],
            confidence=float(item["confidence"]),
            selected_model=item["selected_model"],
            benchmark_score=float(item["benchmark_score"]),
        )

    async def save_feedback_record(self, record: FeedbackRecord) -> None:
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_feedback_record(record)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("feedback record", record.request_id)

    async def load_feedback_records(self) -> list[FeedbackRecord]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("feedback_record")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("feedback_record"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            records = []
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                records.append(self.deserialize_feedback_record(item))
            return records
        except Exception:
            logger.warning("Failed to load feedback records from DynamoDB", exc_info=True)
            return []

    # --- APIKey persistence ---

    @staticmethod
    def _api_key_primary_key(
        key_id: str,
        tenant_id: str | None,
    ) -> dict[str, str]:
        if tenant_id is None:
            return {"PK": f"APIKEY#{key_id}", "SK": "APIKEY"}
        return {
            "PK": f"TENANT#{tenant_id}#APIKEY#{key_id}",
            "SK": "METADATA",
        }

    @staticmethod
    def _api_key_project_partition(
        project_id: str,
        tenant_id: str | None,
    ) -> str:
        if tenant_id is None:
            return f"PROJECT#{project_id}"
        return f"TENANT#{tenant_id}#PROJECT#{project_id}"

    @staticmethod
    def _api_key_epoch_key(tenant_id: str | None) -> dict[str, str]:
        if tenant_id is None:
            return {"PK": "REVOCATION", "SK": "EPOCH"}
        return {"PK": f"TENANT#{tenant_id}", "SK": "AUTHZ#EPOCH"}

    @staticmethod
    def _serialize_dynamo_map(values: dict) -> dict:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        return {name: serializer.serialize(value) for name, value in values.items()}

    @staticmethod
    def _api_key_transaction_token(action: str) -> str:
        import secrets

        # Unique per application attempt, but stable across botocore's retries
        # of this one request. Reusing a key-derived token would make a second
        # concurrent call look like an idempotent success instead of a conflict.
        return f"{action}-{secrets.token_hex(14)}"

    @staticmethod
    def _api_key_condition_failed(exc: Exception, item_index: int) -> bool:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error")
        if not isinstance(error, dict):
            return False
        if error.get("Code") == "ConditionalCheckFailedException":
            return True
        if error.get("Code") != "TransactionCanceledException":
            return False
        reasons = response.get("CancellationReasons")
        return (
            isinstance(reasons, list)
            and len(reasons) > item_index
            and isinstance(reasons[item_index], dict)
            and reasons[item_index].get("Code") == "ConditionalCheckFailed"
        )

    @staticmethod
    def serialize_api_key(key: APIKey) -> dict:
        item = {
            **DynamoPersistence._api_key_primary_key(
                key.key_id,
                key.tenant_id,
            ),
            "entity_type": "api_key",
            "key_id": key.key_id,
            "key_hash": key.key_hash,
            "project_id": key.project_id,
            "name": key.name,
            "scopes": json.dumps(key.scopes),
            "created_by": key.created_by,
            "created_at": key.created_at.isoformat(),
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "revoked": key.revoked,
            "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        }
        if key.tenant_id is not None:
            item["tenant_id"] = key.tenant_id
        return item

    @staticmethod
    def deserialize_api_key(item: dict) -> APIKey:
        scopes_raw = item.get("scopes", "[]")
        scopes = json.loads(scopes_raw) if isinstance(scopes_raw, str) else scopes_raw
        return APIKey(
            key_id=item["key_id"],
            key_hash=item["key_hash"],
            project_id=item["project_id"],
            name=item["name"],
            scopes=scopes,
            created_by=item["created_by"],
            tenant_id=item.get("tenant_id"),
            created_at=datetime.fromisoformat(item["created_at"]),
            expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
            revoked=bool(item.get("revoked", False)),
            revoked_at=datetime.fromisoformat(item["revoked_at"]) if item.get("revoked_at") else None,
            last_used_at=datetime.fromisoformat(item["last_used_at"]) if item.get("last_used_at") else None,
        )

    async def save_api_key(self, key: APIKey) -> None:
        if not self._enabled:
            return

        def _put() -> None:
            table = self._get_table()
            item = self.serialize_api_key(key)
            primary_key = self._api_key_primary_key(key.key_id, key.tenant_id)
            hash_lookup = {
                "PK": f"APIKEY_HASH#{key.key_hash}",
                "SK": "LOOKUP",
                "entity_type": "api_key_hash_lookup",
                "key_id": key.key_id,
                "key_pk": primary_key["PK"],
                "key_sk": primary_key["SK"],
            }
            project_edge = {
                "PK": self._api_key_project_partition(
                    key.project_id,
                    key.tenant_id,
                ),
                "SK": f"APIKEY#{key.key_id}",
                "entity_type": "project_api_key",
                "key_id": key.key_id,
                "project_id": key.project_id,
                "key_pk": primary_key["PK"],
                "key_sk": primary_key["SK"],
            }
            if key.tenant_id is not None:
                hash_lookup["tenant_id"] = key.tenant_id
                project_edge["tenant_id"] = key.tenant_id

            condition = "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                # Compatibility for existing in-process table fakes. Every
                # boto3 DynamoDB Table has meta.client and therefore cannot take
                # this non-transactional branch.
                for row in (item, hash_lookup, project_edge):
                    table.put_item(Item=row)
                return
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                            "ConditionExpression": condition,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(hash_lookup),
                            "ConditionExpression": condition,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(project_edge),
                            "ConditionExpression": condition,
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token("issue"),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise RuntimeError("API key already exists") from exc
            if self._api_key_condition_failed(exc, 1):
                raise RuntimeError("API key hash already exists") from exc
            if self._api_key_condition_failed(exc, 2):
                raise RuntimeError("API key project edge already exists") from exc
            self._record_write_failure("API key", key.key_id)
            raise RuntimeError("API key transaction failed") from exc

    async def get_api_key_by_hash(self, key_hash: str) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            lookup_response = table.get_item(
                Key={"PK": f"APIKEY_HASH#{key_hash}", "SK": "LOOKUP"},
                ConsistentRead=True,
            )
            lookup = lookup_response.get("Item")
            if not lookup:
                return None
            key_id = lookup.get("key_id")
            tenant_id = lookup.get("tenant_id")
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("API key hash lookup has no key_id")
            if tenant_id is not None and (
                not isinstance(tenant_id, str) or not tenant_id
            ):
                raise ValueError("API key hash lookup has an invalid tenant_id")
            key = self._api_key_primary_key(key_id, tenant_id)
            if (
                ("key_pk" in lookup and lookup["key_pk"] != key["PK"])
                or ("key_sk" in lookup and lookup["key_sk"] != key["SK"])
            ):
                raise ValueError("API key hash lookup points outside its namespace")
            key_response = table.get_item(Key=key, ConsistentRead=True)
            item = key_response.get("Item")
            if item is not None and (
                item.get("PK") != key["PK"]
                or item.get("SK") != key["SK"]
                or item.get("key_id") != key_id
                or item.get("tenant_id") != tenant_id
            ):
                raise ValueError(
                    "API key hash lookup returned a mismatched key row"
                )
            return item

        try:
            item = await asyncio.to_thread(_get)
            if item:
                key = self.deserialize_api_key(
                    self._convert_decimals_to_native(item)
                )
                if key.key_hash != key_hash:
                    raise ValueError("API key hash lookup returned a mismatched key")
                return key
        except Exception:
            logger.warning("Failed to lookup API key by hash", exc_info=True)
        return None

    async def get_api_key(
        self,
        key_id: str,
        tenant_id: str | None = None,
    ) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key=self._api_key_primary_key(key_id, tenant_id),
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                key = self.deserialize_api_key(
                    self._convert_decimals_to_native(item)
                )
                if key.key_id != key_id or key.tenant_id != tenant_id:
                    raise ValueError("API key row does not match its storage key")
                return key
        except Exception:
            logger.warning(
                "Failed to get API key %s for tenant %s",
                key_id,
                tenant_id,
                exc_info=True,
            )
        return None

    async def list_api_keys_for_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> list[APIKey]:
        if not self._enabled:
            return []

        def _query():
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(
                    self._api_key_project_partition(project_id, tenant_id)
                )
                & Key("SK").begins_with("APIKEY#"),
                ConsistentRead=True,
            )
            keys = []
            for edge in resp.get("Items", []):
                key_id = edge.get("key_id")
                if not isinstance(key_id, str) or not key_id:
                    raise ValueError("project API key edge has no key_id")
                key = self._api_key_primary_key(key_id, tenant_id)
                if (
                    ("key_pk" in edge and edge["key_pk"] != key["PK"])
                    or ("key_sk" in edge and edge["key_sk"] != key["SK"])
                ):
                    raise ValueError(
                        "project API key edge points outside its namespace"
                    )
                key_resp = table.get_item(Key=key, ConsistentRead=True)
                item = key_resp.get("Item")
                if item:
                    keys.append(item)
            return keys

        try:
            items = await asyncio.to_thread(_query)
            keys = [
                self.deserialize_api_key(self._convert_decimals_to_native(item))
                for item in items
            ]
            if any(
                key.project_id != project_id or key.tenant_id != tenant_id
                for key in keys
            ):
                raise ValueError("project API key edge returned a mismatched key")
            return keys
        except Exception:
            logger.warning(
                "Failed to list API keys for project %s in tenant %s",
                project_id,
                tenant_id,
                exc_info=True,
            )
            return []

    async def update_api_key(self, key: APIKey) -> None:
        if not key.revoked:
            raise ValueError("only revocation updates are supported")
        if not await self.revoke_api_key(key):
            raise RuntimeError("API key is missing or already revoked")

    async def revoke_api_key(self, key: APIKey) -> bool:
        """Atomically revoke a key and advance its cache-invalidation epoch."""
        if not self._enabled:
            return False
        if not key.revoked or key.revoked_at is None:
            raise ValueError("revocation requires revoked=True and revoked_at")

        def _revoke() -> None:
            table = self._get_table()
            primary_key = self._api_key_primary_key(key.key_id, key.tenant_id)
            epoch_key = self._api_key_epoch_key(key.tenant_id)
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                # See save_api_key: this preserves old table-only test doubles;
                # a real DynamoDB Table always takes the transaction below.
                table.put_item(Item=self.serialize_api_key(key))
                update_item = getattr(table, "update_item", None)
                if update_item is not None:
                    update_item(
                        Key=epoch_key,
                        UpdateExpression="ADD #epoch :one",
                        ExpressionAttributeNames={"#epoch": "epoch"},
                        ExpressionAttributeValues={":one": 1},
                    )
                return
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(primary_key),
                            "UpdateExpression": (
                                "SET revoked = :true, revoked_at = :revoked_at"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) AND attribute_exists(SK) "
                                "AND key_hash = :key_hash "
                                "AND (attribute_not_exists(revoked) "
                                "OR revoked = :false)"
                            ),
                            "ExpressionAttributeValues": self._serialize_dynamo_map(
                                {
                                    ":true": True,
                                    ":false": False,
                                    ":revoked_at": key.revoked_at.isoformat(),
                                    ":key_hash": key.key_hash,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(epoch_key),
                            "UpdateExpression": "ADD #epoch :one",
                            "ExpressionAttributeNames": {"#epoch": "epoch"},
                            "ExpressionAttributeValues": self._serialize_dynamo_map(
                                {":one": 1}
                            ),
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token("revoke"),
            )

        try:
            await asyncio.to_thread(_revoke)
            return True
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                return False
            self._record_write_failure("API key revocation", key.key_id)
            raise RuntimeError("API key revocation transaction failed") from exc

    # --- Cross-instance revocation signal ---
    #
    # A revocation has to reach instances that are holding the key in their own
    # validation cache, and there is no message bus here to push it to them. So
    # instead of broadcasting, one counter in the table is bumped on every
    # revocation and each instance polls it cheaply: a changed value means "some
    # key you may be caching was revoked", and the instance drops its cache.
    #
    # Deliberately one counter per tenant rather than a per-key tombstone. It
    # costs one small point read per active tenant and poll interval no matter
    # how many keys exist. Legacy unqualified keys retain their global counter.

    async def bump_revocation_epoch(self, tenant_id: str | None = None) -> None:
        """Signal that a key was revoked. Called on the revoking instance."""
        if not self._enabled:
            return

        def _bump():
            table = self._get_table()
            table.update_item(
                Key=self._api_key_epoch_key(tenant_id),
                UpdateExpression="ADD #epoch :one",
                ExpressionAttributeNames={"#epoch": "epoch"},
                ExpressionAttributeValues={":one": 1},
            )

        try:
            await asyncio.to_thread(_bump)
        except Exception:
            # Logged, not raised: the revocation itself already persisted, and
            # failing the request would tell the operator the revocation did not
            # happen when it did. Other instances fall back to CACHE_TTL_SECONDS.
            self._record_write_failure("revocation_epoch", "EPOCH")

    async def get_revocation_epoch(
        self,
        tenant_id: str | None = None,
    ) -> int | None:
        """Current revocation counter, or None if it could not be read.

        None is distinct from 0: 0 means "no revocation has ever happened", while
        None means the read failed and the caller should keep whatever it already
        believed rather than treat the epoch as reset.
        """
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key=self._api_key_epoch_key(tenant_id),
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return 0
            return int(item.get("epoch", 0))
        except Exception:
            logger.warning("Failed to read revocation epoch", exc_info=True)
            return None

    # --- Fleet-wide spend counters ---
    #
    # Budget enforcement compares spend against a limit, so the spend it reads has
    # to be the whole fleet's. Every instance accumulating its own counter meant a
    # $100 limit admitted roughly $100 *per task* — ~$200 with the shipped
    # desired_count=2, ~$1000 once auto-scaling reached 10 — because no instance
    # ever saw more than its own share.
    #
    # DynamoDB's ADD is atomic and returns the post-update value, which is the
    # whole trick here: the instance that records spend learns the fleet total as
    # a side effect of a write it was already making. No extra read, no lock
    # across instances, and no lost update when two tasks bill at once.

    async def add_spend(self, scope: str, ident: str, cost: float) -> float | None:
        """Atomically add to a spend counter and return the new fleet-wide total.

        ``scope`` is ``"project"`` or ``"user"``; ``ident`` the id within it.

        Returns None if the counter could not be updated, which the caller must
        treat as "no fleet total available" and fall back to its local figure —
        not as zero, which would read as a reset budget and let every request
        through.
        """
        if not self._enabled:
            return None

        def _add():
            table = self._get_table()
            resp = table.update_item(
                Key={"PK": f"SPEND#{scope}#{ident}", "SK": "TOTAL"},
                UpdateExpression="ADD #s :c",
                ExpressionAttributeNames={"#s": "spend"},
                ExpressionAttributeValues={":c": Decimal(str(cost))},
                ReturnValues="UPDATED_NEW",
            )
            return resp.get("Attributes", {}).get("spend")

        try:
            total = await asyncio.to_thread(_add)
            return float(total) if total is not None else None
        except Exception:
            # Logged and surfaced through last_write_error rather than raised: a
            # provider call should not 500 because the counter write failed, and
            # the caller degrades to its local total.
            self._record_write_failure(f"spend_{scope}", ident)
            return None

    async def get_spend(self, scope: str, ident: str) -> float | None:
        """Read a fleet-wide spend counter, or None if it could not be read.

        Used to seed an instance at startup and to answer admin reads. Not called
        per request — `add_spend` already returns the total the request path
        needs.

        None is distinct from 0.0: 0.0 means the counter exists and nothing has
        been spent, while None means the read failed and the caller should keep
        whatever total it already had.
        """
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key={"PK": f"SPEND#{scope}#{ident}", "SK": "TOTAL"},
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                # Absent, not zero — the distinction this method's contract
                # promises. It used to return 0.0 here, which made "no counter
                # yet" look like "nothing spent" and defeated every caller's
                # None check. That is unsafe in the fail-open direction: a
                # project whose counter has not been created (demo seed bills
                # with share=False; reset_spend deletes the item) would read as
                # $0 and reopen a budget gate the local total knows is closed.
                return None
            return float(item.get("spend", 0))
        except Exception:
            logger.warning("Failed to read %s spend for %s", scope, ident, exc_info=True)
            return None

    async def reset_spend(self, scope: str, ident: str) -> bool:
        """Zero a fleet-wide spend counter. Returns whether it succeeded.

        Deletes the item rather than writing 0, so `ADD` recreates it on the next
        charge — the same end state with one fewer value to keep consistent.

        Unlike the writes above this reports failure to the caller: a reset is an
        explicit operator action through `POST /admin/quotas/{id}/reset`, and
        reporting success for a counter that is still at its old value would tell
        an operator a project was unblocked when it is still blocked.
        """
        if not self._enabled:
            return False

        def _delete():
            table = self._get_table()
            table.delete_item(Key={"PK": f"SPEND#{scope}#{ident}", "SK": "TOTAL"})

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            self._record_write_failure(f"spend_reset_{scope}", ident)
            return False

    # --- PolicyNode persistence ---

    @staticmethod
    def serialize_policy_node(node: PolicyNode) -> dict:
        return {
            "PK": f"POLICY_NODE#{node.node_id}",
            "SK": "CONFIG",
            "entity_type": "policy_node",
            "node_id": node.node_id,
            "node_type": node.node_type,
            "parent_id": node.parent_id,
            "display_name": node.display_name,
            "limits": json.dumps(node.limits),
            "created_at": node.created_at.isoformat(),
        }

    @staticmethod
    def deserialize_policy_node(item: dict) -> PolicyNode:
        limits_raw = item.get("limits", "{}")
        limits = json.loads(limits_raw) if isinstance(limits_raw, str) else limits_raw
        return PolicyNode(
            node_id=item["node_id"],
            node_type=item["node_type"],
            parent_id=item.get("parent_id"),
            display_name=item.get("display_name", item["node_id"]),
            limits=limits,
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else datetime.now(timezone.utc),
        )

    async def save_policy_node(self, node: PolicyNode) -> None:
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_policy_node(node)
            table.put_item(Item=item)
            if node.parent_id:
                table.put_item(Item={
                    "PK": f"POLICY_NODE#{node.parent_id}",
                    "SK": f"CHILD#{node.node_id}",
                    "node_id": node.node_id,
                })

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("policy node", node.node_id)

    async def get_policy_node(self, node_id: str) -> PolicyNode | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(Key={"PK": f"POLICY_NODE#{node_id}", "SK": "CONFIG"})
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                return self.deserialize_policy_node(item)
        except Exception:
            logger.warning("Failed to get policy node %s", node_id, exc_info=True)
        return None

    async def load_all_policy_nodes(self) -> list[PolicyNode]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("policy_node")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("policy_node"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            return [self.deserialize_policy_node(item) for item in raw_items]
        except Exception:
            logger.warning("Failed to load policy nodes from DynamoDB", exc_info=True)
            return []

    # --- Cedar policy persistence ---
    #
    # Distinct from PolicyNode above, which is the cost/quota hierarchy. These are
    # the Cedar authorization statements written through POST /admin/policies. The
    # policy's ``name`` is its identity, matching that route's update-by-name
    # behaviour, so re-submitting a name overwrites rather than duplicating.

    @staticmethod
    def serialize_cedar_policy(policy: dict) -> dict:
        return {
            "PK": f"CEDAR_POLICY#{policy['name']}",
            "SK": "CONFIG",
            "entity_type": "cedar_policy",
            "name": policy["name"],
            "description": policy.get("description", ""),
            "policy_text": policy.get("policy_text", ""),
            "mode": policy.get("mode", "LOG_ONLY"),
        }

    @staticmethod
    def deserialize_cedar_policy(item: dict) -> dict:
        return {
            "name": item["name"],
            "description": item.get("description", ""),
            "policy_text": item.get("policy_text", ""),
            # Defaulting to LOG_ONLY on a missing attribute keeps a malformed item
            # from silently becoming enforcing.
            "mode": item.get("mode", "LOG_ONLY"),
        }

    async def save_cedar_policy(self, policy: dict) -> None:
        if not self._enabled:
            return

        def _put():
            self._get_table().put_item(Item=self.serialize_cedar_policy(policy))

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("cedar policy", policy.get("name", "?"))
            # Don't advertise a change that isn't in the table: a bumped version
            # would make every other instance re-scan and adopt a set that does
            # not include this policy, reporting a successful reload of the old
            # rules.
            return
        await self.bump_policy_version()

    async def bump_policy_version(self) -> int | None:
        """Atomically increment the shared policy version, returning the new one.

        The signal other instances poll instead of re-scanning the policy table
        on every request: one small ``GetItem`` per instance per window, and a
        full reload only when the number actually moves. Same ``ADD`` pattern as
        the spend counters, and atomic for the same reason — two operators
        writing different policies concurrently must produce two distinct
        versions, or one write is invisible to the fleet.
        """
        if not self._enabled:
            return None

        def _add():
            resp = self._get_table().update_item(
                Key={"PK": "CEDAR_POLICY#VERSION", "SK": "TOTAL"},
                UpdateExpression="ADD #v :one",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":one": Decimal("1")},
                ReturnValues="UPDATED_NEW",
            )
            return resp.get("Attributes", {}).get("version")

        try:
            version = await asyncio.to_thread(_add)
            return int(version) if version is not None else None
        except Exception:
            self._record_write_failure("cedar policy version", "TOTAL")
            return None

    async def get_policy_version(self) -> int | None:
        """Read the shared policy version, or None if it could not be read.

        Absent returns 0, not None: no policy has ever been written through the
        API, which is a *known* state and the one every gateway starts in.
        Conflating it with "unreadable" would mean the caller never records a
        successful check, so it would re-read this counter on every single request
        for the whole life of a deployment that only uses seed-file policies.

        None is reserved for a genuine read failure, so the caller can keep
        enforcing what it has and retry rather than advance its clock.
        """
        if not self._enabled:
            return None

        def _get():
            resp = self._get_table().get_item(
                Key={"PK": "CEDAR_POLICY#VERSION", "SK": "TOTAL"}
            )
            item = resp.get("Item")
            return item.get("version", 0) if item else 0

        try:
            return int(await asyncio.to_thread(_get))
        except Exception:
            logger.warning("Failed to read the shared policy version", exc_info=True)
            return None

    async def load_all_cedar_policies_or_none(self) -> list[dict] | None:
        """Like ``load_all_cedar_policies``, but None on failure instead of ``[]``.

        The original returns ``[]`` on failure so a Dynamo outage cannot block
        startup, accepting that the Cedar layer fails open. That trade is wrong
        for a live reload: adopting ``[]`` would *drop every policy the fleet is
        enforcing* because a scan timed out, turning a read failure into a
        fleet-wide authorization bypass.
        """
        if not self._enabled:
            return None
        self._last_policy_scan_failed = False
        policies = await self.load_all_cedar_policies()
        return None if self._last_policy_scan_failed else policies

    async def load_all_cedar_policies(self) -> list[dict]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(FilterExpression=Attr("entity_type").eq("cedar_policy"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("cedar_policy"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            policies = [self.deserialize_cedar_policy(item) for item in raw_items]
            self._last_policy_scan_failed = False
            return policies
        except Exception:
            # Returning [] rather than raising keeps a Dynamo outage from blocking
            # startup. It does mean booting with no policies, which — because an
            # ungoverned action is ALLOW — fails open on the Cedar layer while
            # auth, admin RBAC, and quotas stay enforced. Logged at ERROR because
            # that difference matters to whoever reads the boot log.
            #
            # A live reload must NOT accept that trade, which is what
            # ``load_all_cedar_policies_or_none`` is for: dropping the enforced
            # set because a scan failed is a bypass, not a degraded boot.
            logger.error("Failed to load Cedar policies from DynamoDB", exc_info=True)
            self._last_policy_scan_failed = True
            return []

    # --- Event destinations (webhooks / SNS / CloudWatch) ---
    #
    # Written through POST and DELETE /admin/webhooks. A destination's ``name`` is
    # its identity, matching the dispatcher's own remove-by-name behaviour.
    #
    # Stored as one item holding the whole set, not a row per destination —
    # verified necessary, not stylistic. With a row each, a *deletion* is
    # unrepresentable whenever demo seeding is on: the delete removes the row, the
    # next boot re-seeds the destination, and there is no row left to say "an
    # operator removed this". A deleted destination silently resumed receiving
    # security events. The whole-set item makes the stored list authoritative, so
    # a delete is a rewrite that survives.
    #
    # This is the same reasoning as the region topology below, and it also means
    # "saved but empty" is distinguishable from "nothing saved" — hence the
    # ``| None`` return rather than a bare list.

    @staticmethod
    def serialize_event_destinations(destinations: list[dict]) -> dict:
        """Serialize the full destination set ({name, destination_type, ...} each)."""
        return {
            "PK": "EVENT_DESTINATIONS",
            "SK": "CONFIG",
            "entity_type": "event_destination",
            # json.dumps rather than a native list of maps so `event_filter=None`
            # (meaning "every event type") survives the round trip: DynamoDB drops
            # empty/None attributes, which would make an unfiltered destination
            # indistinguishable from one filtered to nothing.
            "destinations": json.dumps([
                {
                    "name": d["name"],
                    "destination_type": d.get("destination_type", "webhook"),
                    "config": d.get("config", {}),
                    "event_filter": d.get("event_filter"),
                    "enabled": bool(d.get("enabled", True)),
                }
                for d in destinations
            ]),
        }

    async def save_event_destinations(self, destinations: list[dict]) -> None:
        """Write the whole destination set. Every add and delete rewrites it."""
        if not self._enabled:
            return

        def _put():
            self._get_table().put_item(
                Item=self.serialize_event_destinations(destinations))

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("event destinations", "set")

    async def load_event_destinations(self) -> list[dict] | None:
        """Return the stored destination set, or None if none was ever saved.

        None means "fall back to seeded/config destinations"; ``[]`` means an
        operator deliberately removed every destination and nothing should be
        restored over the top.
        """
        if not self._enabled:
            return None

        def _get():
            return self._get_table().get_item(
                Key={"PK": "EVENT_DESTINATIONS", "SK": "CONFIG"}
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception:
            # Booting on seed-only destinations means security events may be
            # dispatched somewhere an operator had changed — the gateway serves
            # traffic and the alerting is quietly wrong, so this is an ERROR.
            logger.error("Failed to load event destinations from DynamoDB", exc_info=True)
            return None
        if not item:
            return None
        raw = item.get("destinations", "[]")
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return [
            {
                "name": d["name"],
                "destination_type": d.get("destination_type", "webhook"),
                "config": d.get("config", {}),
                "event_filter": d.get("event_filter"),
                "enabled": bool(d.get("enabled", True)),
            }
            for d in parsed
        ]

    # --- Multi-region topology (hub config + spokes) ---
    #
    # Written through PUT /admin/regions/config and the /admin/regions/spokes
    # routes. Stored as one item rather than a row per spoke: the hub-level
    # settings and the spoke list are edited as a unit, a spoke's
    # ``failover_priority`` is only meaningful relative to its siblings, and a
    # partial read of a topology would route traffic on a set of regions no
    # operator configured.
    #
    # ``status`` is deliberately not persisted. It is health-check state, not
    # configuration; restoring a stale UNHEALTHY would keep a recovered region out
    # of rotation until the next probe, and a stale HEALTHY would send traffic to a
    # region that is still down. Spokes come back at their dataclass default and
    # the first health check corrects it.

    @staticmethod
    def serialize_region_topology(config) -> dict:
        return {
            "PK": "REGION_TOPOLOGY",
            "SK": "CONFIG",
            "entity_type": "region_topology",
            "hub_region": config.hub_region,
            "health_check_interval_seconds": config.health_check_interval_seconds,
            "failover_threshold_consecutive": config.failover_threshold_consecutive,
            "failover_cooldown_seconds": config.failover_cooldown_seconds,
            "data_residency_strict": bool(config.data_residency_strict),
            "spokes": json.dumps([
                {
                    "region": s.region,
                    "role": s.role.value,
                    "weight": s.weight,
                    "endpoint": s.endpoint,
                    "providers": s.providers,
                    "models": s.models,
                    "data_residency_zones": s.data_residency_zones,
                    "health_check_url": s.health_check_url,
                    "max_latency_ms": s.max_latency_ms,
                    "failover_priority": s.failover_priority,
                }
                for s in config.spokes
            ]),
        }

    async def save_region_topology(self, config) -> None:
        if not self._enabled:
            return

        def _put():
            self._get_table().put_item(Item=self.serialize_region_topology(config))

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("region topology", config.hub_region)

    async def load_region_topology(self) -> dict | None:
        """Return the stored topology as a dict, or None if none was saved.

        None and "saved but empty" are different states: the first means fall back
        to the config file, the second means an operator removed every spoke.
        """
        if not self._enabled:
            return None

        def _get():
            return self._get_table().get_item(
                Key={"PK": "REGION_TOPOLOGY", "SK": "CONFIG"}
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception:
            logger.error("Failed to load region topology from DynamoDB", exc_info=True)
            return None
        if not item:
            return None
        spokes_raw = item.get("spokes", "[]")
        return {
            "hub_region": item.get("hub_region", ""),
            "health_check_interval_seconds": int(item.get("health_check_interval_seconds", 30)),
            "failover_threshold_consecutive": int(item.get("failover_threshold_consecutive", 3)),
            "failover_cooldown_seconds": int(item.get("failover_cooldown_seconds", 60)),
            "data_residency_strict": bool(item.get("data_residency_strict", False)),
            "spokes": json.loads(spokes_raw) if isinstance(spokes_raw, str) else spokes_raw,
        }

    # --- SCIM identity (users + groups) ---

    @staticmethod
    def _serialize_scim_user(user: ScimUser) -> dict:
        return {
            "PK": f"SCIM#USER#{user.id}", "SK": "SCIM_USER",
            "entity_type": "scim_user",
            "id": user.id, "user_name": user.user_name, "active": user.active,
            "external_id": user.external_id or "", "display_name": user.display_name,
            "emails": user.emails, "groups": user.groups, "roles": user.roles,
            "project_id": user.project_id,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }

    @staticmethod
    def _deserialize_scim_user(item: dict) -> ScimUser:
        from datetime import datetime as _dt
        return ScimUser(
            id=item["id"], user_name=item["user_name"], active=bool(item.get("active", True)),
            external_id=item.get("external_id") or None,
            display_name=item.get("display_name", ""),
            emails=list(item.get("emails", []) or []),
            groups=list(item.get("groups", []) or []),
            roles=list(item.get("roles", []) or []),
            project_id=item.get("project_id", ""),
            created_at=_dt.fromisoformat(item["created_at"]) if item.get("created_at") else datetime.now(timezone.utc),
            updated_at=_dt.fromisoformat(item["updated_at"]) if item.get("updated_at") else datetime.now(timezone.utc),
        )

    @staticmethod
    def _serialize_scim_group(group: ScimGroup) -> dict:
        return {
            "PK": f"SCIM#GROUP#{group.id}", "SK": "SCIM_GROUP",
            "entity_type": "scim_group",
            "id": group.id, "display_name": group.display_name,
            "external_id": group.external_id or "",
            "members": group.members, "roles": group.roles,
            "created_at": group.created_at.isoformat(),
            "updated_at": group.updated_at.isoformat(),
        }

    @staticmethod
    def _deserialize_scim_group(item: dict) -> ScimGroup:
        from datetime import datetime as _dt
        return ScimGroup(
            id=item["id"], display_name=item["display_name"],
            external_id=item.get("external_id") or None,
            members=list(item.get("members", []) or []),
            roles=list(item.get("roles", []) or []),
            created_at=_dt.fromisoformat(item["created_at"]) if item.get("created_at") else datetime.now(timezone.utc),
            updated_at=_dt.fromisoformat(item["updated_at"]) if item.get("updated_at") else datetime.now(timezone.utc),
        )

    async def save_scim_user(self, user: ScimUser) -> None:
        if not self._enabled:
            return
        item = self._serialize_scim_user(user)
        try:
            await asyncio.to_thread(lambda: self._get_table().put_item(Item=item))
        except Exception:
            self._record_write_failure("SCIM user", user.id)

    async def save_scim_group(self, group: ScimGroup) -> None:
        if not self._enabled:
            return
        item = self._serialize_scim_group(group)
        try:
            await asyncio.to_thread(lambda: self._get_table().put_item(Item=item))
        except Exception:
            self._record_write_failure("SCIM group", group.id)

    async def delete_scim_user(self, user_id: str) -> None:
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(
                lambda: self._get_table().delete_item(
                    Key={"PK": f"SCIM#USER#{user_id}", "SK": "SCIM_USER"}))
        except Exception:
            self._record_write_failure("SCIM user delete", user_id)

    async def delete_scim_group(self, group_id: str) -> None:
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(
                lambda: self._get_table().delete_item(
                    Key={"PK": f"SCIM#GROUP#{group_id}", "SK": "SCIM_GROUP"}))
        except Exception:
            self._record_write_failure("SCIM group delete", group_id)

    async def load_scim_users(self) -> list[ScimUser]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr
            table = self._get_table()
            items, response = [], table.scan(
                FilterExpression=Attr("entity_type").eq("scim_user"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("scim_user"),
                    ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            return [self._deserialize_scim_user(self._convert_decimals_to_native(i)) for i in raw]
        except Exception:
            logger.warning("Failed to load SCIM users from DynamoDB", exc_info=True)
            return []

    async def load_scim_groups(self) -> list[ScimGroup]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr
            table = self._get_table()
            items, response = [], table.scan(
                FilterExpression=Attr("entity_type").eq("scim_group"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("scim_group"),
                    ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            return [self._deserialize_scim_group(self._convert_decimals_to_native(i)) for i in raw]
        except Exception:
            logger.warning("Failed to load SCIM groups from DynamoDB", exc_info=True)
            return []

    # --- Generic item write (used by audit trail) ---

    async def put_item(self, item: dict) -> None:
        """Write a raw item to DynamoDB. Used by subsystems that manage their own schema."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            table.put_item(Item=item)

        await asyncio.to_thread(_put)
