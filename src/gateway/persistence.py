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
        return {
            "PK": f"PROJECT#{project.project_id}",
            "SK": "PROJECT",
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
            return records
        except Exception:
            logger.warning("Failed to load usage records from DynamoDB", exc_info=True)
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

    async def load_projects(self) -> dict[str, Project]:
        """Scan DynamoDB for all projects and deserialize them."""
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
                projects[project.project_id] = project
            return projects
        except Exception:
            logger.warning("Failed to load projects from DynamoDB", exc_info=True)
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
            return configs
        except Exception:
            logger.warning("Failed to load user configs from DynamoDB", exc_info=True)
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
    def serialize_api_key(key: APIKey) -> dict:
        return {
            "PK": f"APIKEY#{key.key_id}",
            "SK": "APIKEY",
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
            created_at=datetime.fromisoformat(item["created_at"]),
            expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
            revoked=bool(item.get("revoked", False)),
            revoked_at=datetime.fromisoformat(item["revoked_at"]) if item.get("revoked_at") else None,
            last_used_at=datetime.fromisoformat(item["last_used_at"]) if item.get("last_used_at") else None,
        )

    async def save_api_key(self, key: APIKey) -> None:
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_api_key(key)
            table.put_item(Item=item)
            # Hash lookup index
            table.put_item(Item={
                "PK": f"APIKEY_HASH#{key.key_hash}",
                "SK": "LOOKUP",
                "key_id": key.key_id,
            })
            # Project membership
            table.put_item(Item={
                "PK": f"PROJECT#{key.project_id}",
                "SK": f"APIKEY#{key.key_id}",
                "key_id": key.key_id,
            })

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("API key", key.key_id)

    async def get_api_key_by_hash(self, key_hash: str) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(Key={"PK": f"APIKEY_HASH#{key_hash}", "SK": "LOOKUP"})
            item = resp.get("Item")
            if not item:
                return None
            key_id = item["key_id"]
            key_resp = table.get_item(Key={"PK": f"APIKEY#{key_id}", "SK": "APIKEY"})
            return key_resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                return self.deserialize_api_key(item)
        except Exception:
            logger.warning("Failed to lookup API key by hash", exc_info=True)
        return None

    async def get_api_key(self, key_id: str) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(Key={"PK": f"APIKEY#{key_id}", "SK": "APIKEY"})
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                return self.deserialize_api_key(item)
        except Exception:
            logger.warning("Failed to get API key %s", key_id, exc_info=True)
        return None

    async def list_api_keys_for_project(self, project_id: str) -> list[APIKey]:
        if not self._enabled:
            return []

        def _query():
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"PROJECT#{project_id}") & Key("SK").begins_with("APIKEY#")
            )
            key_ids = [item["key_id"] for item in resp.get("Items", [])]
            keys = []
            for kid in key_ids:
                key_resp = table.get_item(Key={"PK": f"APIKEY#{kid}", "SK": "APIKEY"})
                item = key_resp.get("Item")
                if item:
                    keys.append(item)
            return keys

        try:
            items = await asyncio.to_thread(_query)
            return [self.deserialize_api_key(item) for item in items]
        except Exception:
            logger.warning("Failed to list API keys for project %s", project_id, exc_info=True)
            return []

    async def update_api_key(self, key: APIKey) -> None:
        await self.save_api_key(key)

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
            return [self.deserialize_cedar_policy(item) for item in raw_items]
        except Exception:
            # Returning [] rather than raising keeps a Dynamo outage from blocking
            # startup. It does mean booting with no policies, which — because an
            # ungoverned action is ALLOW — fails open on the Cedar layer while
            # auth, admin RBAC, and quotas stay enforced. Logged at ERROR because
            # that difference matters to whoever reads the boot log.
            logger.error("Failed to load Cedar policies from DynamoDB", exc_info=True)
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
