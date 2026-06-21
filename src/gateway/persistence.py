"""DynamoDB persistence layer for LLM Router state.

Controlled by LLM_ROUTER_DYNAMODB_ENABLED env var (default: "false").
Uses a single DynamoDB table with composite keys (PK/SK pattern).
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from decimal import Decimal

from src.gateway.models import FeedbackRecord, GuardrailRule, Project, UsageRecord

logger = logging.getLogger(__name__)


class DynamoPersistence:
    """DynamoDB persistence layer for LLM Router state."""

    def __init__(
        self,
        table_name: str = "llm-router-state",
        region: str = "us-east-1",
    ):
        self._table_name = table_name
        self._region = region
        self._enabled = os.environ.get(
            "LLM_ROUTER_DYNAMODB_ENABLED", "false"
        ).lower() == "true"
        self._table = None
        self._dynamodb = None

    @property
    def enabled(self) -> bool:
        """Whether DynamoDB persistence is active."""
        return self._enabled

    def _get_table(self):
        """Lazily create the boto3 DynamoDB Table resource."""
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
            "image_tokens": record.image_tokens,
            "reasoning_tokens": record.reasoning_tokens,
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
            image_tokens=int(item.get("image_tokens", 0)),
            reasoning_tokens=int(item.get("reasoning_tokens", 0)),
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
            log_level=item.get("log_level", "INFO"),
            log_destination=item.get("log_destination"),
            ltm_enabled=bool(item.get("ltm_enabled", False)),
            retention_period_hours=int(item.get("retention_period_hours", 24)),
            rate_limit_rpm=int(item["rate_limit_rpm"]) if item.get("rate_limit_rpm") is not None else None,
            members=members,
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else datetime.utcnow(),
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
            logger.warning("Failed to save usage record %s", record.request_id, exc_info=True)

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
            logger.warning("Failed to save project %s", project.project_id, exc_info=True)

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
            logger.warning("Failed to save user config for %s", user_id, exc_info=True)

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
            logger.warning("Failed to save feedback record %s", record.request_id, exc_info=True)

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
