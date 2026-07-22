"""Tests for PII redaction engine."""

import pytest

from src.gateway.models import ResolvedPolicy
from src.gateway.security.pii_redactor import (
    PII_PATTERNS,
    PIIRedactor,
    RedactionMapping,
    env_default_enabled,
    env_default_types,
)


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


class TestEnvDefault:
    """AXON_PII_REDACTION_DEFAULT makes a deploy safe-by-default (#17)."""

    def test_env_unset_keeps_optin_off(self, redactor, monkeypatch):
        monkeypatch.delenv("AXON_PII_REDACTION_DEFAULT", raising=False)
        assert env_default_enabled() is False
        policy = ResolvedPolicy()  # nothing configured
        result, mapping = redactor.redact("email me at a@b.com", policy)
        assert result == "email me at a@b.com"
        assert mapping.redacted_count == 0

    def test_env_on_redacts_unconfigured_policy(self, redactor, monkeypatch):
        monkeypatch.setenv("AXON_PII_REDACTION_DEFAULT", "true")
        assert env_default_enabled() is True
        policy = ResolvedPolicy()  # policy leaves redaction off
        result, mapping = redactor.redact("email me at a@b.com", policy)
        assert "a@b.com" not in result
        assert mapping.redacted_count == 1

    def test_explicit_policy_off_is_respected_over_env(self, redactor, monkeypatch):
        # effective_policy only fills in when a policy DOESN'T enable redaction;
        # env can turn it on, but an explicit type-limited policy still wins.
        monkeypatch.setenv("AXON_PII_REDACTION_DEFAULT", "on")
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["ssn"])
        # email present but only ssn configured → email must NOT be redacted
        result, mapping = redactor.redact("a@b.com and 123-45-6789", policy)
        assert "a@b.com" in result
        assert "123-45-6789" not in result

    def test_env_default_types_narrowing(self, monkeypatch):
        monkeypatch.setenv("AXON_PII_REDACT_TYPES", "email, ssn")
        assert env_default_types() == ["email", "ssn"]

    def test_env_default_types_unknown_ignored(self, monkeypatch):
        monkeypatch.setenv("AXON_PII_REDACT_TYPES", "email, bogus")
        assert env_default_types() == ["email"]

    def test_env_default_types_all_when_unset(self, monkeypatch):
        monkeypatch.delenv("AXON_PII_REDACT_TYPES", raising=False)
        assert set(env_default_types()) == set(PII_PATTERNS.keys())

    def test_embed_safe_env_unset_passes_through(self, redactor, monkeypatch):
        """Ostiari embed: env NOT set → effective_policy is a no-op, so the
        embedded agent never double-redacts what Ostiari already tokenized."""
        monkeypatch.delenv("AXON_PII_REDACTION_DEFAULT", raising=False)
        policy = ResolvedPolicy()
        # simulate Ostiari having already redacted → content carries tokens
        msgs = [{"role": "user", "content": "contact [EMAIL_1] now"}]
        result, mapping = redactor.redact_messages(msgs, policy)
        assert result[0]["content"] == "contact [EMAIL_1] now"
        assert mapping.redacted_count == 0


class TestTokenNotReRedacted:
    """A second redaction pass over already-tokenized text is a no-op."""

    def test_tokens_are_not_pii_shaped(self, redactor, monkeypatch):
        monkeypatch.setenv("AXON_PII_REDACTION_DEFAULT", "true")
        policy = ResolvedPolicy()
        once, m1 = redactor.redact("mail a@b.com, ssn 123-45-6789", policy)
        twice, m2 = redactor.redact(once, policy)
        assert once == twice           # idempotent
        assert m2.redacted_count == 0  # nothing left to redact


class TestMultimodalContent:
    """redact_messages must reach text parts inside list/multimodal content."""

    def test_openai_style_text_parts(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "reach me at a@b.com"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        }]
        result, mapping = redactor.redact_messages(msgs, policy)
        parts = result[0]["content"]
        assert "a@b.com" not in parts[0]["text"]
        assert "[EMAIL_1]" in parts[0]["text"]
        assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png"}}
        assert mapping.redacted_count == 1

    def test_bedrock_style_text_field(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["ssn"])
        msgs = [{"role": "user", "content": [{"text": "ssn 123-45-6789"}]}]
        result, mapping = redactor.redact_messages(msgs, policy)
        assert "123-45-6789" not in result[0]["content"][0]["text"]

    def test_non_text_parts_untouched(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["email"])
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]
        result, mapping = redactor.redact_messages(msgs, policy)
        assert result[0]["content"][0] == {"type": "image_url", "image_url": {"url": "u"}}
        assert mapping.redacted_count == 0


class TestInternationalPatterns:
    def test_iban(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["iban"])
        result, mapping = redactor.redact("acct DE89370400440532013000 ok", policy)
        assert "DE89370400440532013000" not in result
        assert mapping.redacted_count == 1

    def test_ipv6(self, redactor):
        policy = ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=["ipv6"])
        result, mapping = redactor.redact("host 2001:db8:85a3:0:0:8a2e:370:7334 up", policy)
        assert "[IPV6_1]" in result


class TestNoReinjectMode:
    """Permanent-redaction mode: no plaintext retained, no reinject."""

    def test_no_plaintext_retained(self, redactor):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True, pii_redact_types=["email"], pii_reinject=False)
        result, mapping = redactor.redact("mail a@b.com", policy)
        assert "a@b.com" not in result
        assert mapping.redacted_count == 1
        assert mapping._forward == {}      # nothing stored
        assert mapping._dedup == {}        # sealed after redaction

    def test_reinject_is_noop_when_permanent(self, redactor):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True, pii_redact_types=["email"], pii_reinject=False)
        _, mapping = redactor.redact("mail a@b.com", policy)
        assert redactor.reinject_response("saw [EMAIL_1]", mapping) == "saw [EMAIL_1]"

    def test_reversible_mode_still_reinjects(self, redactor):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True, pii_redact_types=["email"], pii_reinject=True)
        _, mapping = redactor.redact("mail a@b.com", policy)
        assert redactor.reinject_response("saw [EMAIL_1]", mapping) == "saw a@b.com"

    def test_dedup_still_works_in_permanent_mode(self, redactor):
        policy = ResolvedPolicy(
            pii_redaction_enabled=True, pii_redact_types=["email"], pii_reinject=False)
        result, mapping = redactor.redact("a@b.com and again a@b.com", policy)
        assert result.count("[EMAIL_1]") == 2
        assert mapping.redacted_count == 1
