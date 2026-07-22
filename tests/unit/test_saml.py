"""Tests for SAML 2.0 SSO (#14): signed-assertion verification + attribute map.

Generates a self-signed IdP cert and signs assertions with signxml, then checks
that SamlService accepts a valid assertion and rejects tampered / expired /
wrong-audience ones.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.auth.saml_service import SamlConfig, SamlError, SamlService

# signxml/lxml/cryptography are required for these tests (dev extra installs them).
signxml = pytest.importorskip("signxml")
pytest.importorskip("lxml")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from lxml import etree  # noqa: E402
from signxml import XMLSigner, methods  # noqa: E402

SP_ENTITY = "axonllm-sp"


@pytest.fixture(scope="module")
def idp():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


def _make_response(idp, *, audience=SP_ENTITY, not_after_min=5, email="alice@corp.com",
                   groups=("admin", "developer")) -> str:
    key_pem, cert_pem = idp
    now = datetime.now(timezone.utc)
    na = (now + timedelta(minutes=not_after_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    group_vals = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    xml = (
        '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="_a1" Version="2.0" IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        '<saml:Issuer>https://idp.example.com</saml:Issuer>'
        '<saml:Subject><saml:NameID>alice@corp.com</saml:NameID></saml:Subject>'
        f'<saml:Conditions NotOnOrAfter="{na}"><saml:AudienceRestriction>'
        f'<saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>'
        '</saml:Conditions>'
        '<saml:AttributeStatement>'
        '<saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress">'
        f'<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>'
        '<saml:Attribute Name="http://schemas.xmlsoap.org/claims/Group">'
        f'{group_vals}</saml:Attribute>'
        '</saml:AttributeStatement></saml:Assertion>'
    )
    root = etree.fromstring(xml.encode())
    signed = XMLSigner(methods.enveloped, signature_algorithm="rsa-sha256",
                       digest_algorithm="sha256").sign(root, key=key_pem, cert=cert_pem)
    return base64.b64encode(etree.tostring(signed)).decode()


@pytest.fixture
def service(idp):
    _, cert_pem = idp
    return SamlService(SamlConfig(
        sp_entity_id=SP_ENTITY, acs_url="https://sp/acs",
        idp_sso_url="https://idp.example.com/sso",
        idp_entity_id="https://idp.example.com", idp_x509_cert=cert_pem))


class TestVerification:
    def test_valid_assertion_maps_to_context(self, service, idp):
        ctx = service.handle_acs(_make_response(idp))
        assert ctx.user_id == "alice@corp.com"
        assert ctx.email == "alice@corp.com"
        assert ctx.roles == ["admin", "developer"]
        assert ctx.auth_method.value == "sso"

    def test_tampered_content_rejected(self, service, idp):
        good = base64.b64decode(_make_response(idp))
        tampered = base64.b64encode(good.replace(b"alice@corp.com", b"evil@x.com")).decode()
        with pytest.raises(SamlError):
            service.handle_acs(tampered)

    def test_expired_assertion_rejected(self, service, idp):
        with pytest.raises(SamlError, match="expired"):
            service.handle_acs(_make_response(idp, not_after_min=-1))

    def test_wrong_audience_rejected(self, service, idp):
        with pytest.raises(SamlError, match="audience"):
            service.handle_acs(_make_response(idp, audience="some-other-sp"))

    def test_wrong_cert_rejected(self, idp):
        # A service configured with a DIFFERENT cert must reject the assertion.
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil")])
        other_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                      .public_key(other_key.public_key())
                      .serial_number(x509.random_serial_number())
                      .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
                      .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
                      .sign(other_key, hashes.SHA256()))
        svc = SamlService(SamlConfig(
            sp_entity_id=SP_ENTITY, idp_sso_url="x",
            idp_x509_cert=other_cert.public_bytes(serialization.Encoding.PEM).decode()))
        with pytest.raises(SamlError):
            svc.handle_acs(_make_response(idp))

    def test_not_base64_rejected(self, service):
        with pytest.raises(SamlError):
            service.handle_acs("!!!not base64!!!")

    def test_doctype_xxe_rejected(self, service):
        # A DOCTYPE (entity-expansion / XXE vector) must be refused before verify.
        evil = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '<saml:Issuer>&xxe;</saml:Issuer></saml:Assertion>'
        )
        payload = base64.b64encode(evil.encode()).decode()
        with pytest.raises(SamlError):
            service.handle_acs(payload)


class TestConfigAndFlows:
    def test_disabled_without_config(self):
        svc = SamlService(SamlConfig())
        assert svc.enabled is False
        with pytest.raises(SamlError, match="not configured"):
            svc.handle_acs("x")

    def test_authn_request_redirect(self, service):
        url = service.build_authn_request(relay_state="/dashboard")
        assert url.startswith("https://idp.example.com/sso?")
        assert "SAMLRequest=" in url
        assert "RelayState=" in url

    def test_sp_metadata_contains_acs(self, service):
        md = service.sp_metadata()
        assert "https://sp/acs" in md
        assert SP_ENTITY in md
        assert "WantAssertionsSigned=\"true\"" in md
