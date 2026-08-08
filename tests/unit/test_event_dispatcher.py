"""Tests for event dispatcher."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from src.gateway.security.event_dispatcher import (
    DestinationValidationError,
    DestinationType,
    EventDestination,
    EventDispatcher,
    SecurityEvent,
)

_PUBLIC_IPV4 = "93.184.216.34"
_PUBLIC_IPV6 = "2606:4700:4700::1111"
_WEBHOOK_URL = "https://webhook.example.com/events"
_OUTBOX_QUEUE_URL = (
    "https://sqs.us-east-1.amazonaws.com/123456789012/security-events.fifo"
)
_OUTBOX_QUEUE_ARN = (
    "arn:aws:sqs:us-east-1:123456789012:security-events.fifo"
)


def _run(coro):
    return asyncio.run(coro)


async def _public_resolver(hostname, port):
    assert hostname
    assert port > 0
    return (_PUBLIC_IPV4,)


class _RecordingHTTPClient:
    def __init__(self, status_code=204):
        self.calls = []
        self.closed = False
        self.status_code = status_code

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status_code)

    async def aclose(self):
        self.closed = True


class _RecordingSQSClient:
    def __init__(self):
        self.attribute_calls = []
        self.delete_calls = []
        self.receive_calls = []
        self.receive_gate = None
        self.receive_responses = []
        self.send_calls = []
        self.send_error = None
        self.visibility_calls = []

    def send_message(self, **kwargs):
        self.send_calls.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return {"MessageId": f"sent-{len(self.send_calls)}"}

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        if self.receive_gate is not None:
            self.receive_gate.wait(timeout=2)
        if self.receive_responses:
            return self.receive_responses.pop(0)
        return {}

    def delete_message(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {}

    def change_message_visibility(self, **kwargs):
        self.visibility_calls.append(kwargs)
        return {}

    def get_queue_attributes(self, **kwargs):
        self.attribute_calls.append(kwargs)
        return {
            "Attributes": {
                "QueueArn": _OUTBOX_QUEUE_ARN,
                "FifoQueue": "true",
            }
        }


@pytest.fixture
def dispatcher():
    instance = EventDispatcher(
        resolver=_public_resolver,
        aws_region="us-east-1",
        aws_account_id="123456789012",
    )
    instance._http_client = _RecordingHTTPClient()
    return instance


def _outbox_dispatcher(sqs_client, *, http_status=204):
    instance = EventDispatcher(
        resolver=_public_resolver,
        aws_region="us-east-1",
        aws_account_id="123456789012",
        outbox_queue_url=_OUTBOX_QUEUE_URL,
        sqs_client=sqs_client,
    )
    instance._http_client = _RecordingHTTPClient(status_code=http_status)
    return instance


def _tenant_event(event_id="event-1"):
    return SecurityEvent(
        event_id=event_id,
        event_type="test",
        timestamp="2026-01-01T00:00:00Z",
        tenant_id="tenant-a",
        data={"reason": "test"},
    )


def _tenant_webhook_destination(**config):
    return EventDestination(
        tenant_id="tenant-a",
        name="alerts",
        destination_type=DestinationType.WEBHOOK,
        config={"url": _WEBHOOK_URL, **config},
    )


def _received_message(send_call, *, receive_count="1", receipt="receipt-1"):
    return {
        "Body": send_call["MessageBody"],
        "ReceiptHandle": receipt,
        "MessageId": "message-1",
        "Attributes": {"ApproximateReceiveCount": receive_count},
    }


class TestDestinationManagement:
    def test_add_destination(self, dispatcher):
        dest = EventDestination(
            name="slack",
            destination_type=DestinationType.WEBHOOK,
            config={"url": "https://hooks.slack.com/test"},
        )
        dispatcher.add_destination(dest)
        assert len(dispatcher.destinations) == 1
        assert dispatcher.destinations[0].name == "slack"

    def test_remove_destination(self, dispatcher):
        dest = EventDestination(name="test", destination_type=DestinationType.WEBHOOK)
        dispatcher.add_destination(dest)
        assert dispatcher.remove_destination("test") is True
        assert len(dispatcher.destinations) == 0

    def test_remove_nonexistent(self, dispatcher):
        assert dispatcher.remove_destination("nope") is False

    def test_multiple_destinations(self, dispatcher):
        for name in ("slack", "datadog", "pagerduty"):
            dispatcher.add_destination(EventDestination(name=name, destination_type=DestinationType.WEBHOOK))
        assert len(dispatcher.destinations) == 3


class TestEventFiltering:
    def test_disabled_destination_skipped(self, dispatcher):
        dest = EventDestination(
            name="disabled",
            destination_type=DestinationType.WEBHOOK,
            config={"url": "http://localhost"},
            enabled=False,
        )
        dispatcher.add_destination(dest)

        event = SecurityEvent(
            event_id="e1",
            event_type="injection_blocked",
            timestamp="2024-01-01T00:00:00Z",
        )
        _run(dispatcher.dispatch(event))
        assert dispatcher._dispatch_count == 0

    def test_event_filter_respects_type(self, dispatcher):
        dest = EventDestination(
            name="injection-only",
            destination_type=DestinationType.WEBHOOK,
            config={"url": _WEBHOOK_URL},
            event_filter=["injection_blocked"],
        )
        dispatcher.add_destination(dest)

        # PII event should be skipped
        event = SecurityEvent(
            event_id="e1",
            event_type="pii_redaction",
            timestamp="2024-01-01T00:00:00Z",
        )
        _run(dispatcher.dispatch(event))
        assert dispatcher._dispatch_count == 0

    def test_matching_filter_dispatches(self, dispatcher):
        dest = EventDestination(
            name="all-events",
            destination_type=DestinationType.WEBHOOK,
            config={"url": _WEBHOOK_URL},
            event_filter=None,  # No filter = receive all
        )
        dispatcher.add_destination(dest)

        event = SecurityEvent(
            event_id="e1",
            event_type="injection_blocked",
            timestamp="2024-01-01T00:00:00Z",
        )
        _run(dispatcher.dispatch(event))
        assert dispatcher._dispatch_count == 1


class TestSecurityEventHelpers:
    def test_dispatch_injection_event(self, dispatcher):
        dest = EventDestination(
            name="test",
            destination_type=DestinationType.WEBHOOK,
            config={"url": _WEBHOOK_URL},
        )
        dispatcher.add_destination(dest)

        _run(
            dispatcher.dispatch_injection_event(
                event_id="e1",
                user_id="u1",
                project_id="p1",
                threat_level="high",
                patterns=["role_override"],
                blocked=True,
            )
        )
        assert dispatcher._dispatch_count == 1

    def test_dispatch_pii_event(self, dispatcher):
        dest = EventDestination(
            name="test",
            destination_type=DestinationType.WEBHOOK,
            config={"url": _WEBHOOK_URL},
        )
        dispatcher.add_destination(dest)

        _run(
            dispatcher.dispatch_pii_event(
                event_id="e2",
                user_id="u1",
                project_id="p1",
                redacted_types=["email", "ssn"],
                count=3,
            )
        )
        assert dispatcher._dispatch_count == 1


class TestEventSerialization:
    def test_to_dict(self):
        event = SecurityEvent(
            event_id="e1",
            event_type="injection_blocked",
            timestamp="2024-01-01T00:00:00Z",
            severity="critical",
            user_id="user-1",
            project_id="proj-1",
            data={"patterns": ["role_override"]},
        )
        d = event.to_dict()
        assert d["event_id"] == "e1"
        assert d["severity"] == "critical"
        assert d["data"]["patterns"] == ["role_override"]

    def test_tenant_id_is_serialized(self):
        event = SecurityEvent(
            event_id="e1",
            event_type="test",
            timestamp="2026-01-01T00:00:00Z",
            tenant_id="tenant-a",
        )

        assert event.to_dict()["tenant_id"] == "tenant-a"


class TestStats:
    def test_initial_stats(self, dispatcher):
        stats = dispatcher.stats
        assert stats["destinations"] == 0
        assert stats["dispatched"] == 0
        assert stats["errors"] == 0

    def test_stats_after_dispatch(self, dispatcher):
        dest = EventDestination(
            name="x",
            destination_type=DestinationType.WEBHOOK,
            config={"url": _WEBHOOK_URL},
        )
        dispatcher.add_destination(dest)
        event = SecurityEvent(event_id="e1", event_type="test", timestamp="t")
        _run(dispatcher.dispatch(event))

        stats = dispatcher.stats
        assert stats["destinations"] == 1
        assert stats["dispatched"] == 1


class TestTenantIsolation:
    def test_same_destination_name_is_isolated_between_tenants(self, dispatcher):
        for tenant_id in ("tenant-a", "tenant-b"):
            dispatcher.add_destination(
                EventDestination(
                    tenant_id=tenant_id,
                    name="alerts",
                    destination_type=DestinationType.WEBHOOK,
                    config={"url": _WEBHOOK_URL},
                )
            )

        _run(
            dispatcher.dispatch(
                SecurityEvent(
                    event_id="event-a",
                    event_type="test",
                    timestamp="2026-01-01T00:00:00Z",
                    tenant_id="tenant-a",
                )
            )
        )

        assert dispatcher.stats_for_tenant("tenant-a")["dispatched"] == 1
        assert dispatcher.stats_for_tenant("tenant-b")["dispatched"] == 0
        assert dispatcher.stats_for_tenant("tenant-a")["destinations"] == 1
        assert dispatcher.stats_for_tenant("tenant-b")["destinations"] == 1

    def test_direct_cross_tenant_send_is_refused(self, dispatcher):
        destination = EventDestination(
            tenant_id="tenant-a",
            name="alerts",
            destination_type=DestinationType.WEBHOOK,
        )
        event = SecurityEvent(
            event_id="event-b",
            event_type="test",
            timestamp="2026-01-01T00:00:00Z",
            tenant_id="tenant-b",
        )

        with pytest.raises(ValueError, match="cross-tenant"):
            _run(dispatcher._send_to_destination(event, destination))


def _webhook_destination(url, **config):
    return EventDestination(
        name="alerts",
        destination_type=DestinationType.WEBHOOK,
        config={"url": url, **config},
    )


class TestWebhookDestinationSecurity:
    @pytest.mark.parametrize(
        "url",
        [
            "",
            "http://webhook.example.com/events",
            "https://user:secret@webhook.example.com/events",
            "https://localhost/events",
            "https://metadata.internal/events",
            "https://hooks.example.test/events",
            "https://bad_host.example.com/events",
            "https://%31%32%37.0.0.1/events",
            "https://webhook.example.com/events#fragment",
            "https://webhook.example.com\\@127.0.0.1/events",
        ],
    )
    def test_rejects_malformed_or_non_public_urls(self, dispatcher, url):
        with pytest.raises(DestinationValidationError):
            _run(dispatcher.validate_destination(_webhook_destination(url)))

    @pytest.mark.parametrize(
        "url",
        [
            "https://0.0.0.0/events",
            "https://127.0.0.1/events",
            "https://10.0.0.1/events",
            "https://169.254.169.254/latest/meta-data",
            "https://224.0.0.1/events",
            "https://240.0.0.1/events",
            "https://[::]/events",
            "https://[::1]/events",
            "https://[fc00::1]/events",
            "https://[fe80::1]/events",
            "https://[ff02::1]/events",
            "https://[2001:db8::1]/events",
            "https://[::ffff:127.0.0.1]/events",
        ],
    )
    def test_rejects_non_global_ipv4_and_ipv6_literals(self, dispatcher, url):
        with pytest.raises(DestinationValidationError, match="non-public"):
            _run(dispatcher.validate_destination(_webhook_destination(url)))

    @pytest.mark.parametrize(
        "url",
        [
            "https://2130706433/events",
            "https://0x7f000001/events",
            "https://0177.0.0.1/events",
            "https://127.1/events",
        ],
    )
    def test_rejects_legacy_numeric_loopback_forms(self, dispatcher, url):
        with pytest.raises(DestinationValidationError, match="non-public"):
            _run(dispatcher.validate_destination(_webhook_destination(url)))

    def test_rejects_internal_dns_and_mixed_public_private_answers(self):
        async def internal_resolver(hostname, port):
            return ("10.0.0.8", _PUBLIC_IPV4)

        dispatcher = EventDispatcher(resolver=internal_resolver)

        with pytest.raises(DestinationValidationError, match="non-public"):
            _run(
                dispatcher.validate_destination(
                    _webhook_destination(
                        "https://webhook.example.com/events"
                    )
                )
            )

    def test_delivery_connects_to_the_validated_ip_with_host_and_sni(self, dispatcher):
        destination = _webhook_destination(
            "https://webhook.example.com:8443/events?id=7",
            headers={"Authorization": "Bearer secret"},
        )
        event = SecurityEvent(
            event_id="event-1",
            event_type="test",
            timestamp="2026-01-01T00:00:00Z",
        )

        _run(dispatcher._send_to_destination(event, destination))

        [(url, request)] = dispatcher._http_client.calls
        assert url == f"https://{_PUBLIC_IPV4}:8443/events?id=7"
        assert request["headers"]["Host"] == "webhook.example.com:8443"
        assert request["headers"]["Authorization"] == "Bearer secret"
        assert request["headers"]["X-Axon-Event-ID"] == "event-1"
        assert len(request["headers"]["Idempotency-Key"]) == 64
        int(request["headers"]["Idempotency-Key"], 16)
        assert request["extensions"] == {
            "sni_hostname": "webhook.example.com"
        }
        assert request["follow_redirects"] is False

    def test_ipv6_delivery_uses_a_bracketed_validated_address(self):
        async def ipv6_resolver(hostname, port):
            return (_PUBLIC_IPV6,)

        dispatcher = EventDispatcher(resolver=ipv6_resolver)
        dispatcher._http_client = _RecordingHTTPClient()

        _run(
            dispatcher._send_to_destination(
                SecurityEvent(
                    event_id="event-1",
                    event_type="test",
                    timestamp="2026-01-01T00:00:00Z",
                ),
                _webhook_destination(_WEBHOOK_URL),
            )
        )

        assert dispatcher._http_client.calls[0][0] == (
            f"https://[{_PUBLIC_IPV6}]:443/events"
        )

    def test_dispatch_revalidates_dns_and_blocks_rebinding(self):
        answers = iter([(_PUBLIC_IPV4,), ("127.0.0.1",)])

        async def rebinding_resolver(hostname, port):
            return next(answers)

        dispatcher = EventDispatcher(resolver=rebinding_resolver)
        dispatcher._http_client = _RecordingHTTPClient()
        destination = _webhook_destination(_WEBHOOK_URL)
        _run(dispatcher.validate_destination(destination))

        with pytest.raises(RuntimeError, match="delivery failed"):
            _run(
                dispatcher._send_to_destination(
                    SecurityEvent(
                        event_id="event-1",
                        event_type="test",
                        timestamp="2026-01-01T00:00:00Z",
                    ),
                    destination,
                )
            )

        assert dispatcher._http_client.calls == []

    def test_caller_cannot_override_connection_headers(self, dispatcher):
        destination = _webhook_destination(
            _WEBHOOK_URL,
            headers={"Host": "169.254.169.254"},
        )

        with pytest.raises(DestinationValidationError, match="unsafe field"):
            _run(dispatcher.validate_destination(destination))

    @pytest.mark.parametrize(
        "header_name",
        ["Idempotency-Key", "idempotency-key", "X-Axon-Event-ID"],
    )
    def test_caller_cannot_override_idempotency_headers(
        self,
        dispatcher,
        header_name,
    ):
        destination = _webhook_destination(
            _WEBHOOK_URL,
            headers={header_name: "caller-controlled"},
        )

        with pytest.raises(DestinationValidationError, match="unsafe field"):
            _run(dispatcher.validate_destination(destination))


class TestAwsDestinationSecurity:
    def test_accepts_same_account_same_region_sns_topic(self, dispatcher):
        destination = EventDestination(
            name="alerts",
            destination_type=DestinationType.SNS,
            config={
                "topic_arn": (
                    "arn:aws:sns:us-east-1:123456789012:security-alerts"
                )
            },
        )

        _run(dispatcher.validate_destination(destination))

    @pytest.mark.parametrize(
        "topic_arn",
        [
            "arn:aws:sns:us-west-2:123456789012:security-alerts",
            "arn:aws:sns:us-east-1:210987654321:security-alerts",
            "arn:aws-cn:sns:us-east-1:123456789012:security-alerts",
            "arn:aws:sns:us-east-1:123456789012:security-*",
            "arn:aws:sns:us-east-1:123456789012:security-alerts:subscription",
        ],
    )
    def test_rejects_cross_context_or_non_concrete_sns_arns(
        self,
        dispatcher,
        topic_arn,
    ):
        destination = EventDestination(
            name="alerts",
            destination_type=DestinationType.SNS,
            config={"topic_arn": topic_arn},
        )

        with pytest.raises(DestinationValidationError):
            _run(dispatcher.validate_destination(destination))

    def test_fifo_sns_uses_stable_ids_but_standard_sns_omits_them(
        self,
        dispatcher,
        monkeypatch,
    ):
        published = []

        class _SNSClient:
            def publish(self, **kwargs):
                published.append(kwargs)

        def client(service_name, *, region_name):
            assert service_name == "sns"
            assert region_name == "us-east-1"
            return _SNSClient()

        monkeypatch.setattr("boto3.client", client)
        event = SecurityEvent(
            event_id="event-1",
            event_type="test",
            timestamp="2026-01-01T00:00:00Z",
        )
        standard = EventDestination(
            name="standard-alerts",
            destination_type=DestinationType.SNS,
            config={
                "topic_arn": (
                    "arn:aws:sns:us-east-1:123456789012:security-alerts"
                )
            },
        )
        fifo = EventDestination(
            name="fifo-alerts",
            destination_type=DestinationType.SNS,
            config={
                "topic_arn": (
                    "arn:aws:sns:us-east-1:123456789012:security-alerts.fifo"
                )
            },
        )

        _run(dispatcher._send_sns(event, standard))
        _run(dispatcher._send_sns(event, fifo))
        _run(dispatcher._send_sns(event, fifo))

        assert "MessageGroupId" not in published[0]
        assert "MessageDeduplicationId" not in published[0]
        assert published[1]["MessageGroupId"] == published[2]["MessageGroupId"]
        assert (
            published[1]["MessageDeduplicationId"]
            == published[2]["MessageDeduplicationId"]
        )
        assert len(published[1]["MessageGroupId"]) == 64
        assert len(published[1]["MessageDeduplicationId"]) == 64


class TestDurableEventOutbox:
    def test_requires_a_fifo_queue_url(self):
        with pytest.raises(ValueError, match="FIFO"):
            EventDispatcher(
                outbox_queue_url=(
                    "https://sqs.us-east-1.amazonaws.com/"
                    "123456789012/security-events"
                )
            )

    def test_reads_queue_url_from_environment(self, monkeypatch):
        monkeypatch.setenv("AXON_EVENT_OUTBOX_QUEUE_URL", _OUTBOX_QUEUE_URL)

        dispatcher = EventDispatcher(sqs_client=_RecordingSQSClient())

        assert dispatcher.outbox_enabled is True

    def test_enqueue_snapshots_matching_destination_with_stable_ids(self):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs)
        destination = _tenant_webhook_destination(
            headers={"Authorization": "Bearer durable-secret"}
        )
        dispatcher.add_destination(destination)
        dispatcher.add_destination(
            EventDestination(
                tenant_id="tenant-a",
                name="other-events",
                destination_type=DestinationType.WEBHOOK,
                config={"url": _WEBHOOK_URL},
                event_filter=["other"],
            )
        )
        dispatcher.add_destination(
            EventDestination(
                tenant_id="tenant-a",
                name="disabled",
                destination_type=DestinationType.WEBHOOK,
                config={"url": _WEBHOOK_URL},
                enabled=False,
            )
        )
        event = _tenant_event()

        _run(dispatcher.dispatch(event))
        _run(dispatcher.dispatch(event))

        assert len(sqs.send_calls) == 2
        first, second = sqs.send_calls
        assert first["QueueUrl"] == _OUTBOX_QUEUE_URL
        assert first["MessageGroupId"] == second["MessageGroupId"]
        assert (
            first["MessageDeduplicationId"]
            == second["MessageDeduplicationId"]
        )
        assert len(first["MessageGroupId"]) == 64
        assert len(first["MessageDeduplicationId"]) == 64
        int(first["MessageGroupId"], 16)
        int(first["MessageDeduplicationId"], 16)

        envelope = json.loads(first["MessageBody"])
        assert envelope["schema_version"] == 1
        assert envelope["tenant_id"] == "tenant-a"
        assert envelope["delivery_id"] == first["MessageDeduplicationId"]
        assert envelope["event"] == event.to_dict()
        assert envelope["destination"] == {
            "name": "alerts",
            "destination_type": "webhook",
            "config": {
                "url": _WEBHOOK_URL,
                "headers": {"Authorization": "Bearer durable-secret"},
            },
            "event_filter": None,
            "enabled": True,
            "tenant_id": "tenant-a",
        }
        destination.config["url"] = "https://changed.example.com/events"
        assert json.loads(first["MessageBody"])["destination"]["config"]["url"] == (
            _WEBHOOK_URL
        )
        assert dispatcher.stats_for_tenant("tenant-a")["dispatched"] == 0
        assert dispatcher._http_client.calls == []

    def test_enqueue_failure_is_reported_after_updating_error_stats(
        self,
        caplog,
    ):
        sqs = _RecordingSQSClient()
        sqs.send_error = RuntimeError("transport failed")
        dispatcher = _outbox_dispatcher(sqs)
        dispatcher.add_destination(
            _tenant_webhook_destination(
                headers={"Authorization": "Bearer do-not-log"}
            )
        )

        with pytest.raises(RuntimeError, match="durably enqueued"):
            _run(dispatcher.dispatch(_tenant_event()))

        stats = dispatcher.stats_for_tenant("tenant-a")
        assert stats["errors"] == 1
        assert stats["dispatched"] == 0
        assert "do-not-log" not in caplog.text

    def test_worker_delivers_then_deletes_and_counts_success(self):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs)
        dispatcher.add_destination(_tenant_webhook_destination())
        _run(dispatcher.dispatch(_tenant_event()))
        message = _received_message(sqs.send_calls[0])
        sqs.receive_responses.append({"Messages": [message]})

        async def exercise_worker():
            await dispatcher.start()
            try:
                for _ in range(100):
                    if sqs.delete_calls:
                        break
                    await asyncio.sleep(0.005)
                assert sqs.delete_calls
            finally:
                await dispatcher.stop()

        _run(exercise_worker())

        assert len(dispatcher._http_client.calls) == 1
        assert sqs.delete_calls == [
            {
                "QueueUrl": _OUTBOX_QUEUE_URL,
                "ReceiptHandle": "receipt-1",
            }
        ]
        assert sqs.visibility_calls == []
        assert sqs.receive_calls[0]["WaitTimeSeconds"] == 10
        assert dispatcher.stats_for_tenant("tenant-a")["dispatched"] == 1
        assert dispatcher.worker_running is False

    def test_worker_failure_retries_without_delete(self):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs, http_status=503)
        dispatcher.add_destination(_tenant_webhook_destination())
        _run(dispatcher.dispatch(_tenant_event()))
        message = _received_message(sqs.send_calls[0], receive_count="3")

        delivered = _run(dispatcher._process_outbox_message(message))

        assert delivered is False
        assert sqs.delete_calls == []
        assert sqs.visibility_calls == [
            {
                "QueueUrl": _OUTBOX_QUEUE_URL,
                "ReceiptHandle": "receipt-1",
                "VisibilityTimeout": 20,
            }
        ]
        stats = dispatcher.stats_for_tenant("tenant-a")
        assert stats["errors"] == 1
        assert stats["dispatched"] == 0

    def test_malformed_poison_message_is_retained_with_capped_delay(self):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs)
        message = {
            "Body": "{not-json",
            "ReceiptHandle": "poison-receipt",
            "Attributes": {"ApproximateReceiveCount": "100"},
        }

        delivered = _run(dispatcher._process_outbox_message(message))

        assert delivered is False
        assert sqs.delete_calls == []
        assert sqs.visibility_calls == [
            {
                "QueueUrl": _OUTBOX_QUEUE_URL,
                "ReceiptHandle": "poison-receipt",
                "VisibilityTimeout": 300,
            }
        ]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tenant_id", "tenant-b"),
            ("destination_type", "email"),
        ],
    )
    def test_worker_rejects_tenant_mismatch_and_unknown_types(
        self,
        field,
        value,
    ):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs)
        dispatcher.add_destination(_tenant_webhook_destination())
        _run(dispatcher.dispatch(_tenant_event()))
        envelope = json.loads(sqs.send_calls[0]["MessageBody"])
        if field == "tenant_id":
            envelope["destination"]["tenant_id"] = value
        else:
            envelope["destination"]["destination_type"] = value
        message = _received_message(
            {"MessageBody": json.dumps(envelope)},
            receipt=f"receipt-{field}",
        )

        delivered = _run(dispatcher._process_outbox_message(message))

        assert delivered is False
        assert sqs.delete_calls == []
        assert sqs.visibility_calls[0]["ReceiptHandle"] == f"receipt-{field}"

    def test_readiness_checks_attributes_without_consuming(self):
        sqs = _RecordingSQSClient()
        dispatcher = _outbox_dispatcher(sqs)

        assert _run(dispatcher.check_readiness()) is True
        assert sqs.attribute_calls == [
            {
                "QueueUrl": _OUTBOX_QUEUE_URL,
                "AttributeNames": ["QueueArn", "FifoQueue"],
            }
        ]
        assert sqs.receive_calls == []

    def test_stop_cancels_long_poll_and_closes_owned_http_client(self):
        sqs = _RecordingSQSClient()
        sqs.receive_gate = threading.Event()
        dispatcher = _outbox_dispatcher(sqs)
        owned_http_client = _RecordingHTTPClient()
        dispatcher._http_client = owned_http_client
        dispatcher._owns_http_client = True

        async def exercise_lifecycle():
            await dispatcher.start()
            task = dispatcher._worker_task
            await dispatcher.start()
            assert dispatcher._worker_task is task
            try:
                for _ in range(100):
                    if sqs.receive_calls:
                        break
                    await asyncio.sleep(0.005)
                assert sqs.receive_calls
                await dispatcher.stop(timeout_seconds=0.1)
            finally:
                sqs.receive_gate.set()

        _run(exercise_lifecycle())

        assert dispatcher.worker_running is False
        assert owned_http_client.closed is True


class TestAwsDestinationConfiguration:
    def test_aws_destinations_require_configured_account_context(self):
        dispatcher = EventDispatcher(
            resolver=_public_resolver,
            aws_region="us-east-1",
            aws_account_id="",
        )
        destination = EventDestination(
            name="alerts",
            destination_type=DestinationType.SNS,
            config={
                "topic_arn": (
                    "arn:aws:sns:us-east-1:123456789012:security-alerts"
                )
            },
        )

        with pytest.raises(DestinationValidationError, match="account ID"):
            _run(dispatcher.validate_destination(destination))

    def test_accepts_concrete_cloudwatch_log_group_arn(self, dispatcher):
        destination = EventDestination(
            name="audit",
            destination_type=DestinationType.CLOUDWATCH,
            config={
                "log_group_arn": (
                    "arn:aws:logs:us-east-1:123456789012:"
                    "log-group:/axonllm/security"
                ),
                "log_stream": "events",
            },
        )

        _run(dispatcher.validate_destination(destination))

    def test_accepts_cloudwatch_group_arn_with_stream_wildcard_suffix(
        self,
        dispatcher,
    ):
        destination = EventDestination(
            name="audit",
            destination_type=DestinationType.CLOUDWATCH,
            config={
                "log_group_arn": (
                    "arn:aws:logs:us-east-1:123456789012:"
                    "log-group:/axonllm/security:*"
                ),
                "log_stream": "events",
            },
        )

        _run(dispatcher.validate_destination(destination))

    def test_runtime_allowlists_reject_ungranted_aws_destinations(self):
        allowed_topic = (
            "arn:aws:sns:us-east-1:123456789012:security-events.fifo"
        )
        allowed_group = (
            "arn:aws:logs:us-east-1:123456789012:"
            "log-group:/managed/security:*"
        )
        dispatcher = EventDispatcher(
            resolver=_public_resolver,
            aws_region="us-east-1",
            aws_account_id="123456789012",
            allowed_sns_topic_arns=[allowed_topic],
            allowed_log_group_arns=[allowed_group],
        )

        _run(
            dispatcher.validate_destination(
                EventDestination(
                    name="allowed-topic",
                    destination_type=DestinationType.SNS,
                    config={"topic_arn": allowed_topic},
                )
            )
        )
        _run(
            dispatcher.validate_destination(
                EventDestination(
                    name="allowed-logs",
                    destination_type=DestinationType.CLOUDWATCH,
                    config={
                        "log_group": "/managed/security",
                        "log_stream": "events",
                    },
                )
            )
        )

        with pytest.raises(DestinationValidationError, match="allowlist"):
            _run(
                dispatcher.validate_destination(
                    EventDestination(
                        name="other-topic",
                        destination_type=DestinationType.SNS,
                        config={
                            "topic_arn": (
                                "arn:aws:sns:us-east-1:123456789012:"
                                "other-topic"
                            )
                        },
                    )
                )
            )
        with pytest.raises(DestinationValidationError, match="allowlist"):
            _run(
                dispatcher.validate_destination(
                    EventDestination(
                        name="other-logs",
                        destination_type=DestinationType.CLOUDWATCH,
                        config={
                            "log_group": "/other/security",
                            "log_stream": "events",
                        },
                    )
                )
            )

    def test_cloudwatch_creates_missing_stream_and_tolerates_create_race(
        self,
        dispatcher,
        monkeypatch,
    ):
        class _AwsError(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.response = {"Error": {"Code": code}}

        class _LogsClient:
            def __init__(self):
                self.create_calls = []
                self.put_calls = []

            def put_log_events(self, **kwargs):
                self.put_calls.append(kwargs)
                if len(self.put_calls) == 1:
                    raise _AwsError("ResourceNotFoundException")

            def create_log_stream(self, **kwargs):
                self.create_calls.append(kwargs)
                raise _AwsError("ResourceAlreadyExistsException")

        logs_client = _LogsClient()

        def client(service_name, *, region_name):
            assert service_name == "logs"
            assert region_name == "us-east-1"
            return logs_client

        monkeypatch.setattr("boto3.client", client)
        destination = EventDestination(
            name="audit",
            destination_type=DestinationType.CLOUDWATCH,
            config={
                "log_group": "/axonllm/security",
                "log_stream": "tenant-a-events",
            },
        )
        event = SecurityEvent(
            event_id="event-1",
            event_type="test",
            timestamp="2026-01-01T00:00:00Z",
        )

        _run(dispatcher._send_cloudwatch(event, destination))

        assert logs_client.create_calls == [
            {
                "logGroupName": "/axonllm/security",
                "logStreamName": "tenant-a-events",
            }
        ]
        assert len(logs_client.put_calls) == 2
        assert logs_client.put_calls[1]["logGroupName"] == "/axonllm/security"
        assert logs_client.put_calls[1]["logStreamName"] == "tenant-a-events"

    @pytest.mark.parametrize(
        "config",
        [
            {
                "log_group_arn": (
                    "arn:aws:logs:us-east-1:210987654321:"
                    "log-group:/axonllm/security"
                )
            },
            {
                "log_group_arn": (
                    "arn:aws:logs:us-west-2:123456789012:"
                    "log-group:/axonllm/security"
                )
            },
            {
                "log_group_arn": (
                    "arn:aws:logs:us-east-1:123456789012:"
                    "log-group:/axonllm/*"
                )
            },
            {"log_group": "/axonllm/security", "log_stream": "tenant:*"},
        ],
    )
    def test_rejects_cross_context_or_wildcard_cloudwatch_targets(
        self,
        dispatcher,
        config,
    ):
        destination = EventDestination(
            name="audit",
            destination_type=DestinationType.CLOUDWATCH,
            config=config,
        )

        with pytest.raises(DestinationValidationError):
            _run(dispatcher.validate_destination(destination))
