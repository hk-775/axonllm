"""Tests for PII redaction engine."""

import pytest

from src.gateway.models import ResolvedPolicy
from src.gateway.security.pii_redactor import PIIRedactor, RedactionMapping


@pytest.fixture
def redactor():
    return PIIRedactor()


class TestDisabledRedaction:
    def test_no_op_when_disabled(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=False)
        text = "My email is test@example.com"
        result, mapping = redactor.redact(text, policy)
        assert result == text
        assert mapping.redacted_count == 0

    def test_no_op_when_no_types(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=[])
        text = "My SSN is 123-45-6789"
        result, mapping = redactor.redact(text, policy)
        assert result == text
        assert mapping.redacted_count == 0


class TestEmailRedaction:
    def test_redacts_single_email(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        text = "Contact me at john@company.com for details"
        result, mapping = redactor.redact(text, policy)
        assert "john@company.com" not in result
        assert "[EMAIL_1]" in result
        assert mapping.redacted_count == 1

    def test_redacts_multiple_emails(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        text = "Send to alice@corp.io and bob@corp.io"
        result, mapping = redactor.redact(text, policy)
        assert "alice@corp.io" not in result
        assert "bob@corp.io" not in result
        assert "[EMAIL_1]" in result
        assert "[EMAIL_2]" in result
        assert mapping.redacted_count == 2

    def test_deduplicates_same_email(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        text = "Email me at a@b.com, again a@b.com"
        result, mapping = redactor.redact(text, policy)
        assert mapping.redacted_count == 1
        assert result.count("[EMAIL_1]") == 2


class TestSSNRedaction:
    def test_redacts_ssn(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["ssn"])
        text = "My SSN is 123-45-6789"
        result, mapping = redactor.redact(text, policy)
        assert "123-45-6789" not in result
        assert "[SSN_1]" in result

    def test_ignores_non_ssn_numbers(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["ssn"])
        text = "Order #12345 was placed"
        result, mapping = redactor.redact(text, policy)
        assert result == text
        assert mapping.redacted_count == 0


class TestPhoneRedaction:
    def test_redacts_us_phone(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["phone"])
        text = "Call me at (555) 123-4567"
        result, mapping = redactor.redact(text, policy)
        assert "(555) 123-4567" not in result
        assert "[PHONE_1]" in result

    def test_redacts_phone_with_country_code(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["phone"])
        text = "My number: +1-555-123-4567"
        result, mapping = redactor.redact(text, policy)
        assert "+1-555-123-4567" not in result


class TestMultipleTypes:
    def test_redacts_email_and_ssn(self, redactor):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True,
            pii_redact_types=["email", "ssn"],
        )
        text = "User john@x.com has SSN 111-22-3333"
        result, mapping = redactor.redact(text, policy)
        assert "john@x.com" not in result
        assert "111-22-3333" not in result
        assert mapping.redacted_count == 2


class TestMessageRedaction:
    def test_redacts_across_messages(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        messages = [
            {"role": "user", "content": "My email is a@b.com"},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "Also cc c@d.com"},
        ]
        result, mapping = redactor.redact_messages(messages, policy)
        assert "a@b.com" not in result[0]["content"]
        assert "c@d.com" not in result[2]["content"]
        assert mapping.redacted_count == 2


class TestReinjection:
    def test_reinjects_originals(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        text = "Contact john@x.com for help"
        redacted, mapping = redactor.redact(text, policy)

        response = "Sure, I'll contact [EMAIL_1] right away."
        reinjected = redactor.reinject_response(response, mapping)
        assert reinjected == "Sure, I'll contact john@x.com right away."
