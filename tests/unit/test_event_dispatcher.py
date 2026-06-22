"""Tests for event dispatcher."""

import asyncio

import pytest

from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    EventDispatcher,
    SecurityEvent,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def dispatcher():
    return EventDispatcher()


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
            dispatcher.add_destination(
                EventDestination(name=name, destination_type=DestinationType.WEBHOOK)
            )
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
            event_id="e1", event_type="injection_blocked",
            timestamp="2024-01-01T00:00:00Z",
        )
        _run(dispatcher.dispatch(event))
        assert dispatcher._dispatch_count == 0

    def test_event_filter_respects_type(self, dispatcher):
        dest = EventDestination(
            name="injection-only",
            destination_type=DestinationType.WEBHOOK,
            config={"url": ""},
            event_filter=["injection_blocked"],
        )
        dispatcher.add_destination(dest)

        # PII event should be skipped
        event = SecurityEvent(
            event_id="e1", event_type="pii_redaction",
            timestamp="2024-01-01T00:00:00Z",
        )
        _run(dispatcher.dispatch(event))
        assert dispatcher._dispatch_count == 0

    def test_matching_filter_dispatches(self, dispatcher):
        dest = EventDestination(
            name="all-events",
            destination_type=DestinationType.WEBHOOK,
            config={"url": ""},
            event_filter=None,  # No filter = receive all
        )
        dispatcher.add_destination(dest)

        event = SecurityEvent(
            event_id="e1", event_type="injection_blocked",
            timestamp="2024-01-01T00:00:00Z",
        )
        # Will fail at HTTP level (no url) but the dispatch logic runs
        _run(dispatcher.dispatch(event))
        # No url means _send_webhook returns early — counts as success
        assert dispatcher._dispatch_count == 1


class TestSecurityEventHelpers:
    def test_dispatch_injection_event(self, dispatcher):
        dest = EventDestination(
            name="test", destination_type=DestinationType.WEBHOOK,
            config={"url": ""},
        )
        dispatcher.add_destination(dest)

        _run(dispatcher.dispatch_injection_event(
            event_id="e1", user_id="u1", project_id="p1",
            threat_level="high", patterns=["role_override"], blocked=True,
        ))
        assert dispatcher._dispatch_count == 1

    def test_dispatch_pii_event(self, dispatcher):
        dest = EventDestination(
            name="test", destination_type=DestinationType.WEBHOOK,
            config={"url": ""},
        )
        dispatcher.add_destination(dest)

        _run(dispatcher.dispatch_pii_event(
            event_id="e2", user_id="u1", project_id="p1",
            redacted_types=["email", "ssn"], count=3,
        ))
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


class TestStats:
    def test_initial_stats(self, dispatcher):
        stats = dispatcher.stats
        assert stats["destinations"] == 0
        assert stats["dispatched"] == 0
        assert stats["errors"] == 0

    def test_stats_after_dispatch(self, dispatcher):
        dest = EventDestination(
            name="x", destination_type=DestinationType.WEBHOOK,
            config={"url": ""},
        )
        dispatcher.add_destination(dest)
        event = SecurityEvent(event_id="e1", event_type="test", timestamp="t")
        _run(dispatcher.dispatch(event))

        stats = dispatcher.stats
        assert stats["destinations"] == 1
        assert stats["dispatched"] == 1
