"""Correlation-owned security-event delivery and DLQ rehearsal."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import common
from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    SecurityEvent,
    _outbox_message_body,
)


OPERATIONS = framework.DOMAIN_OPERATIONS["security"]
AWS_TIMEOUT_SECONDS = 8.0
DELIVERY_POLLS = 10
RECEIVE_PAGES = 5
_QUEUE_ARN = re.compile(
    r"^arn:aws:sqs:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"(?P<name>[A-Za-z0-9_-]{1,80}\.fifo)$"
)
_ALARM_ARN = re.compile(
    r"^arn:aws:cloudwatch:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"alarm:(?P<name>[A-Za-z0-9_.:/=-]{1,255})$"
)
_LOG_GROUP_ARN = re.compile(
    r"^arn:aws:logs:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"log-group:(?P<name>[.\-_/#A-Za-z0-9]{1,512})$"
)


def _call(
    context: worker.HandlerContext,
    service: str,
    operation: str,
    parameters: Mapping[str, Any],
) -> Mapping[str, Any]:
    context.cancellation.raise_if_cancelled()
    return context.aws.call(
        service,
        operation,
        region=context.region,
        parameters=parameters,
        timeout_seconds=AWS_TIMEOUT_SECONDS,
    )


def _resources(
    task: worker.ActionTask,
    context: worker.HandlerContext,
) -> dict[str, str]:
    parameters = task.payload.get("parameters")
    binding = task.payload.get("binding")
    if not isinstance(parameters, Mapping) or not isinstance(binding, Mapping):
        raise worker.HandlerContractError from None
    values = {
        "outboxQueueArn": parameters.get("outboxQueueArn"),
        "deadLetterQueueArn": parameters.get("deadLetterQueueArn"),
        "deadLetterAlarmArn": parameters.get("deadLetterAlarmArn"),
        "outboxQueueUrl": binding.get("outboxQueueUrl"),
        "deadLetterQueueUrl": binding.get("deadLetterQueueUrl"),
        "securityEventLogGroupArn": binding.get(
            "securityEventLogGroupArn"
        ),
    }
    if any(not isinstance(value, str) for value in values.values()):
        raise worker.HandlerContractError from None
    outbox = _QUEUE_ARN.fullmatch(values["outboxQueueArn"])
    dlq = _QUEUE_ARN.fullmatch(values["deadLetterQueueArn"])
    alarm = _ALARM_ARN.fullmatch(values["deadLetterAlarmArn"])
    log_group = _LOG_GROUP_ARN.fullmatch(
        values["securityEventLogGroupArn"]
    )
    if (
        outbox is None
        or dlq is None
        or alarm is None
        or log_group is None
        or values["outboxQueueArn"] == values["deadLetterQueueArn"]
        or {
            outbox.group("region"),
            dlq.group("region"),
            alarm.group("region"),
            log_group.group("region"),
        }
        != {context.region}
        or len(
            {
                outbox.group("account"),
                dlq.group("account"),
                alarm.group("account"),
                log_group.group("account"),
            }
        )
        != 1
        or not values["outboxQueueUrl"].endswith(
            f"/{outbox.group('name')}"
        )
        or not values["deadLetterQueueUrl"].endswith(
            f"/{dlq.group('name')}"
        )
    ):
        raise worker.HandlerContractError from None
    return {
        **values,
        "alarmName": alarm.group("name"),
        "logGroupName": log_group.group("name"),
    }


def _queue_binding(
    context: worker.HandlerContext,
    *,
    url: str,
    arn: str,
) -> None:
    attributes = _call(
        context,
        "sqs",
        "get_queue_attributes",
        {"QueueUrl": url, "AttributeNames": ["QueueArn"]},
    ).get("Attributes")
    if not isinstance(attributes, Mapping) or attributes.get("QueueArn") != arn:
        raise worker.DomainTaskFailure("SecurityQueueBindingMismatch")


def _queue_state(
    context: worker.HandlerContext,
    *,
    url: str,
    arn: str,
) -> tuple[int, int, int]:
    names = [
        "QueueArn",
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesDelayed",
        "ApproximateNumberOfMessagesNotVisible",
    ]
    attributes = _call(
        context,
        "sqs",
        "get_queue_attributes",
        {"QueueUrl": url, "AttributeNames": names},
    ).get("Attributes")
    if not isinstance(attributes, Mapping) or attributes.get("QueueArn") != arn:
        raise worker.DomainTaskFailure("SecurityQueueBindingMismatch")
    counts: list[int] = []
    for name in names[1:]:
        raw = attributes.get(name)
        if not isinstance(raw, str) or not raw.isdecimal():
            raise worker.DomainTaskFailure(
                "SecurityQueueCountUnavailable",
                retryable=True,
            )
        counts.append(int(raw))
    return counts[0], counts[1], counts[2]


def _stream_name(owner_id: str) -> str:
    return f"axonllm-launch-{owner_id[:32]}"


def _event_id(task: worker.ActionTask, suffix: str) -> str:
    value = f"{task.owner_id}:{suffix}"
    if len(value) > 128:
        raise worker.HandlerContractError from None
    return value


def _message(
    task: worker.ActionTask,
    resources: Mapping[str, str],
    *,
    suffix: str,
) -> tuple[str, str]:
    event_id = _event_id(task, suffix)
    event = SecurityEvent(
        event_id=event_id,
        event_type="launch_rehearsal",
        timestamp=task.expires_at.isoformat(),
        severity="info",
        tenant_id=task.payload["parameters"]["tenantId"],
        user_id="launch-rehearsal",
        project_id=task.payload["parameters"]["projectId"],
        data={
            "correlation_id": task.correlation_id,
            "owner_id": task.owner_id,
            "fixture": suffix,
        },
    )
    destination = EventDestination(
        name=f"launch-{task.owner_id[:24]}",
        destination_type=DestinationType.CLOUDWATCH,
        config={
            "log_group_arn": resources["securityEventLogGroupArn"],
            "log_stream": _stream_name(task.owner_id),
            "region": task.payload["binding"]["region"],
        },
        event_filter=["launch_rehearsal"],
        enabled=True,
        tenant_id=task.payload["parameters"]["tenantId"],
    )
    return event_id, _outbox_message_body(event, destination)


def _ensure_stream(
    context: worker.HandlerContext,
    *,
    log_group: str,
    stream: str,
) -> None:
    try:
        _call(
            context,
            "logs",
            "create_log_stream",
            {"logGroupName": log_group, "logStreamName": stream},
        )
    except worker.AwsTransportError as exc:
        if exc.aws_code != "ResourceAlreadyExistsException":
            raise


def _send(
    context: worker.HandlerContext,
    *,
    url: str,
    body: str,
    owner_id: str,
) -> None:
    response = _call(
        context,
        "sqs",
        "send_message",
        {
            "QueueUrl": url,
            "MessageBody": body,
            "MessageGroupId": owner_id,
            "MessageDeduplicationId": hashlib.sha256(
                body.encode("utf-8")
            ).hexdigest(),
        },
    )
    if not isinstance(response.get("MessageId"), str):
        raise worker.DomainTaskFailure(
            "SecurityFixtureEnqueueUnavailable",
            retryable=True,
        )


def _delivered(
    context: worker.HandlerContext,
    *,
    log_group: str,
    stream: str,
    event_id: str,
) -> bool:
    response = _call(
        context,
        "logs",
        "filter_log_events",
        {
            "logGroupName": log_group,
            "logStreamNames": [stream],
            "filterPattern": f'"{event_id}"',
            "limit": 100,
        },
    )
    events = response.get("events")
    if type(events) is not list or response.get("nextToken"):
        raise worker.DomainTaskFailure(
            "SecurityDeliveryEvidenceUnavailable",
            retryable=True,
        )
    for item in events:
        message = item.get("message") if isinstance(item, Mapping) else None
        if not isinstance(message, str) or len(message) > 256 * 1024:
            continue
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            continue
        if (
            type(value) is dict
            and value.get("event_id") == event_id
            and value.get("event_type") == "launch_rehearsal"
        ):
            return True
    return False


def _wait_delivery(
    context: worker.HandlerContext,
    *,
    log_group: str,
    stream: str,
    event_id: str,
) -> None:
    for attempt in range(DELIVERY_POLLS):
        if _delivered(
            context,
            log_group=log_group,
            stream=stream,
            event_id=event_id,
        ):
            return
        if attempt + 1 < DELIVERY_POLLS:
            context.cancellation.raise_if_cancelled()
            time.sleep(1)
    raise worker.DomainTaskFailure(
        "SecurityDeliveryPending",
        retryable=True,
    )


def _owned_messages(
    context: worker.HandlerContext,
    *,
    url: str,
    expected_bodies: Sequence[str],
) -> list[Mapping[str, Any]]:
    expected = set(expected_bodies)
    if len(expected) != len(expected_bodies):
        raise worker.HandlerContractError from None
    found: dict[str, dict[str, Mapping[str, Any]]] = {
        body: {} for body in expected_bodies
    }
    for _ in range(RECEIVE_PAGES):
        response = _call(
            context,
            "sqs",
            "receive_message",
            {
                "QueueUrl": url,
                "MaxNumberOfMessages": 10,
                "VisibilityTimeout": 0,
                "WaitTimeSeconds": 0,
                "AttributeNames": ["ApproximateReceiveCount"],
            },
        )
        messages = response.get("Messages", [])
        if type(messages) is not list:
            raise worker.DomainTaskFailure(
                "SecurityQueueReadUnavailable",
                retryable=True,
            )
        for message in messages:
            body = message.get("Body") if isinstance(message, Mapping) else None
            receipt = (
                message.get("ReceiptHandle")
                if isinstance(message, Mapping)
                else None
            )
            message_id = (
                message.get("MessageId")
                if isinstance(message, Mapping)
                else None
            )
            if (
                not isinstance(body, str)
                or not isinstance(receipt, str)
                or not isinstance(message_id, str)
                or not message_id
                or len(message_id) > 256
            ):
                raise worker.DomainTaskFailure(
                    "SecurityQueueReadUnavailable",
                    retryable=True,
                )
            if body in expected:
                found[body][message_id] = message
        if all(found[body] for body in expected_bodies):
            break
    return [
        message
        for body in expected_bodies
        for _, message in sorted(found[body].items())
    ]


def _require_owned_absent(
    context: worker.HandlerContext,
    *,
    url: str,
    arn: str,
    expected_bodies: Sequence[str],
    failure_code: str,
) -> None:
    for _ in range(2):
        if _owned_messages(
            context,
            url=url,
            expected_bodies=expected_bodies,
        ):
            raise worker.DomainTaskFailure(
                failure_code,
                retryable=True,
            )
        _, delayed, not_visible = _queue_state(
            context,
            url=url,
            arn=arn,
        )
        if delayed or not_visible:
            raise worker.DomainTaskFailure(
                "SecurityOwnedMessageVisibilityPending",
                retryable=True,
            )


def _delete_messages(
    context: worker.HandlerContext,
    *,
    url: str,
    messages: Sequence[Mapping[str, Any]],
) -> None:
    for message in messages:
        _call(
            context,
            "sqs",
            "delete_message",
            {
                "QueueUrl": url,
                "ReceiptHandle": message["ReceiptHandle"],
            },
        )


class SecurityDomain:
    """Prove delivery and redrive using only correlation-owned fixtures."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        resources = _resources(task, context)
        _queue_binding(
            context,
            url=resources["outboxQueueUrl"],
            arn=resources["outboxQueueArn"],
        )
        _queue_binding(
            context,
            url=resources["deadLetterQueueUrl"],
            arn=resources["deadLetterQueueArn"],
        )
        common.completed_state(
            state,
            operations=OPERATIONS,
            operation=operation,
        )
        stream = _stream_name(task.owner_id)
        fixture_id = common.owned_id(task, "security-event-stream")
        dlq_correlation = common.owned_id(task, "security-event-dlq")

        if operation == "deliver-security-events":
            event_id, body = _message(
                task,
                resources,
                suffix="delivery",
            )
            _ensure_stream(
                context,
                log_group=resources["logGroupName"],
                stream=stream,
            )
            if not _delivered(
                context,
                log_group=resources["logGroupName"],
                stream=stream,
                event_id=event_id,
            ):
                _send(
                    context,
                    url=resources["outboxQueueUrl"],
                    body=body,
                    owner_id=task.owner_id,
                )
            _wait_delivery(
                context,
                log_group=resources["logGroupName"],
                stream=stream,
                event_id=event_id,
            )
            next_ownership = common.copied_ownership(ownership)
            next_ownership["fixtureIds"] = [fixture_id]
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
                extra={
                    **resources,
                    "streamName": stream,
                    "deliveryEventId": event_id,
                    "deliveryBody": body,
                },
            )
            return framework.DomainActionResult(
                evidence={
                    "configuredDestinationCount": 1,
                    "deliveredDestinationCount": 1,
                },
                state=next_state,
                ownership=next_ownership,
            )

        if any(state.get(name) != resources[name] for name in resources):
            raise worker.DomainTaskFailure("SecurityResourceBindingMismatch")
        if operation == "verify-outbox-drained":
            _wait_delivery(
                context,
                log_group=resources["logGroupName"],
                stream=stream,
                event_id=state["deliveryEventId"],
            )
            delivery_body = state.get("deliveryBody")
            if not isinstance(delivery_body, str):
                raise worker.HandlerContractError from None
            _require_owned_absent(
                context,
                url=resources["outboxQueueUrl"],
                arn=resources["outboxQueueArn"],
                expected_bodies=[delivery_body],
                failure_code="SecurityOutboxDrainPending",
            )
            evidence: dict[str, worker.JsonValue] = {
                "outboxMessagesAfterDelivery": 0
            }
            extra = None
        elif operation == "force-dead-letter":
            event_id, body = _message(task, resources, suffix="redelivery")
            messages = _owned_messages(
                context,
                url=resources["deadLetterQueueUrl"],
                expected_bodies=[body],
            )
            if not messages:
                _send(
                    context,
                    url=resources["deadLetterQueueUrl"],
                    body=body,
                    owner_id=task.owner_id,
                )
                messages = _owned_messages(
                    context,
                    url=resources["deadLetterQueueUrl"],
                    expected_bodies=[body],
                )
            if len(messages) != 1:
                raise worker.DomainTaskFailure(
                    "SecurityDlqFixturePending",
                    retryable=True,
                )
            next_ownership = common.copied_ownership(ownership)
            next_ownership["dlqCorrelationIds"] = [dlq_correlation]
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
                extra={
                    "redeliveryEventId": event_id,
                    "redeliveryBody": body,
                },
            )
            return framework.DomainActionResult(
                evidence={"dlqMessagesAfterFailure": 1},
                state=next_state,
                ownership=next_ownership,
            )
        elif operation == "verify-dead-letter-alarm":
            alarms = _call(
                context,
                "cloudwatch",
                "describe_alarms",
                {"AlarmNames": [resources["alarmName"]]},
            ).get("MetricAlarms")
            if (
                type(alarms) is not list
                or len(alarms) != 1
                or alarms[0].get("AlarmArn")
                != resources["deadLetterAlarmArn"]
                or alarms[0].get("StateValue") != "ALARM"
            ):
                raise worker.DomainTaskFailure(
                    "SecurityDlqAlarmPending",
                    retryable=True,
                )
            evidence = {"dlqAlarmState": "ALARM"}
            extra = None
        elif operation == "redrive-dead-letter":
            body = state.get("redeliveryBody")
            if not isinstance(body, str):
                raise worker.HandlerContractError from None
            messages = _owned_messages(
                context,
                url=resources["deadLetterQueueUrl"],
                expected_bodies=[body],
            )
            if messages:
                _send(
                    context,
                    url=resources["outboxQueueUrl"],
                    body=body,
                    owner_id=task.owner_id,
                )
                _delete_messages(
                    context,
                    url=resources["deadLetterQueueUrl"],
                    messages=messages,
                )
            else:
                _require_owned_absent(
                    context,
                    url=resources["deadLetterQueueUrl"],
                    arn=resources["deadLetterQueueArn"],
                    expected_bodies=[body],
                    failure_code="SecurityOwnedRedrivePending",
                )
                if not _delivered(
                    context,
                    log_group=resources["logGroupName"],
                    stream=stream,
                    event_id=state["redeliveryEventId"],
                ):
                    raise worker.DomainTaskFailure(
                        "SecurityOwnedRedrivePending",
                        retryable=True,
                    )
            next_ownership = common.copied_ownership(ownership)
            next_ownership["dlqCorrelationIds"] = []
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
            )
            return framework.DomainActionResult(
                evidence={"redrivenMessageCount": 1},
                state=next_state,
                ownership=next_ownership,
            )
        elif operation == "verify-redelivery":
            _wait_delivery(
                context,
                log_group=resources["logGroupName"],
                stream=stream,
                event_id=state["redeliveryEventId"],
            )
            redelivery_body = state.get("redeliveryBody")
            if not isinstance(redelivery_body, str):
                raise worker.HandlerContractError from None
            _require_owned_absent(
                context,
                url=resources["deadLetterQueueUrl"],
                arn=resources["deadLetterQueueArn"],
                expected_bodies=[redelivery_body],
                failure_code="SecurityOwnedRedrivePending",
            )
            _require_owned_absent(
                context,
                url=resources["outboxQueueUrl"],
                arn=resources["outboxQueueArn"],
                expected_bodies=[redelivery_body],
                failure_code="SecurityRedriveDrainPending",
            )
            evidence = {
                "dlqMessagesAfterRedrive": 0,
                "outboxMessagesAfterRedrive": 0,
            }
            extra = None
        else:
            raise worker.HandlerContractError from None
        next_state = common.completed_state(
            state,
            operations=OPERATIONS,
            operation=operation,
            extra=extra,
        )
        return framework.DomainActionResult(
            evidence=evidence,
            state=next_state,
            ownership=dict(ownership),
        )

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        fixture_id = f"{owner.owner_id}:security-event-stream"
        dlq_correlation = f"{owner.owner_id}:security-event-dlq"
        fixtures = ownership.get("fixtureIds")
        dlq = ownership.get("dlqCorrelationIds")
        if (
            type(fixtures) is not list
            or type(dlq) is not list
            or any(item != fixture_id for item in fixtures)
            or any(item != dlq_correlation for item in dlq)
        ):
            raise worker.HandlerContractError from None
        if not fixtures and not dlq:
            return common.empty_cleanup(state=state, ownership=ownership)
        expected = [
            body
            for body in (
                state.get("deliveryBody"),
                state.get("redeliveryBody"),
            )
            if isinstance(body, str)
        ]
        for url_name in ("outboxQueueUrl", "deadLetterQueueUrl"):
            arn_name = (
                "outboxQueueArn"
                if url_name == "outboxQueueUrl"
                else "deadLetterQueueArn"
            )
            messages = _owned_messages(
                context,
                url=state[url_name],
                expected_bodies=expected,
            )
            _delete_messages(
                context,
                url=state[url_name],
                messages=messages,
            )
            _require_owned_absent(
                context,
                url=state[url_name],
                arn=state[arn_name],
                expected_bodies=expected,
                failure_code="SecurityCleanupPending",
            )
        try:
            _call(
                context,
                "logs",
                "delete_log_stream",
                {
                    "logGroupName": state["logGroupName"],
                    "logStreamName": state["streamName"],
                },
            )
        except worker.AwsTransportError as exc:
            if exc.aws_code != "ResourceNotFoundException":
                raise
        next_ownership = common.copied_ownership(ownership)
        next_ownership["fixtureIds"] = []
        next_ownership["dlqCorrelationIds"] = []
        return framework.DomainCleanupResult(
            state={**dict(state), "cleanupComplete": True},
            ownership=next_ownership,
            verified_complete=True,
            cleared_fixture_ids=list(fixtures),
            removed_dlq_correlation_ids=list(dlq),
        )


def create_domain(**_kwargs: Any) -> SecurityDomain:
    return SecurityDomain()
