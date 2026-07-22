"""SAML 2.0 SSO (SP side) — pure-Python assertion verification.

Validates IdP-signed SAML responses without a libxml2/xmlsec1 system dependency:
signature verification uses ``signxml`` (XML-DSig over lxml + ``cryptography``,
both installed from wheels — no native build step). Safe XML parsing guards
against entity-expansion / external-entity attacks.

Flow:
- ``build_authn_request()`` → SP-initiated redirect to the IdP SSO URL.
- ``handle_acs(saml_response_b64)`` → verify the signed assertion against the
  IdP cert, enforce audience + NotOnOrAfter, map attributes to a RequestContext.
- ``sp_metadata()`` → SP metadata XML for IdP configuration.

Only the signed-assertion (or signed-response) POST binding is supported — the
flow every enterprise IdP uses. Encrypted assertions are not handled.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

from src.gateway.models import AuthMethod, RequestContext

logger = logging.getLogger(__name__)

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}


class SamlError(Exception):
    """Raised when a SAML response is missing, malformed, or fails validation."""


@dataclass
class SamlConfig:
    """SP + IdP settings for SAML SSO."""

    sp_entity_id: str = ""          # our SP identifier (audience the IdP asserts to)
    acs_url: str = ""               # Assertion Consumer Service URL (our /saml/acs)
    idp_entity_id: str = ""         # the IdP's issuer
    idp_sso_url: str = ""           # IdP SSO redirect endpoint
    idp_x509_cert: str = ""         # IdP signing cert (PEM or bare base64 body)
    attribute_mappings: dict = field(default_factory=lambda: {
        "user_id": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
        "email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "roles": "http://schemas.xmlsoap.org/claims/Group",
        "project_id": "project_id",
    })

    @property
    def enabled(self) -> bool:
        return bool(self.idp_sso_url and self.idp_x509_cert and self.sp_entity_id)


def _pem(cert: str) -> str:
    """Normalize an IdP cert to PEM (accept a bare base64 body)."""
    cert = cert.strip()
    if "BEGIN CERTIFICATE" in cert:
        return cert
    body = "".join(cert.split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


class SamlService:
    """SP-side SAML 2.0: build AuthnRequests, verify assertions, expose metadata."""

    def __init__(self, config: SamlConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    # -- SP-initiated login --------------------------------------------------

    def build_authn_request(self, relay_state: str = "") -> str:
        """Return the IdP SSO redirect URL (HTTP-Redirect binding, deflated)."""
        cfg = self._config
        req_id = "_" + secrets.token_hex(16)
        issue_instant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        xml = (
            f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" '
            f'xmlns:saml="{NS["saml"]}" ID="{req_id}" Version="2.0" '
            f'IssueInstant="{issue_instant}" '
            f'AssertionConsumerServiceURL="{cfg.acs_url}" '
            f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f'<saml:Issuer>{cfg.sp_entity_id}</saml:Issuer>'
            f'</samlp:AuthnRequest>'
        )
        # HTTP-Redirect binding: raw-DEFLATE + base64 in the SAMLRequest param.
        deflated = zlib.compress(xml.encode())[2:-4]
        params = {"SAMLRequest": base64.b64encode(deflated).decode()}
        if relay_state:
            params["RelayState"] = relay_state
        sep = "&" if "?" in cfg.idp_sso_url else "?"
        return f"{cfg.idp_sso_url}{sep}{urlencode(params)}"

    # -- ACS: verify and map -------------------------------------------------

    def handle_acs(self, saml_response_b64: str) -> RequestContext:
        """Verify a base64 SAMLResponse and map its assertion to a context.

        Raises SamlError on any failure (bad signature, wrong audience, expired).
        """
        if not self._config.enabled:
            raise SamlError("SAML is not configured")
        try:
            raw = base64.b64decode(saml_response_b64)
        except Exception as e:
            raise SamlError(f"SAMLResponse is not valid base64: {e}") from e

        verified = self._verify_signature(raw)
        self._check_conditions(verified)
        return self._map_attributes(verified)

    def _verify_signature(self, raw: bytes):
        """Verify the XML-DSig against the configured IdP cert; return the root.

        Uses signxml (XML-DSig, exclusive C14N) — verification fails closed on a
        broken/absent signature, protecting against signature-wrapping.
        """
        try:
            from lxml import etree
            from signxml import XMLVerifier
        except ImportError as e:  # pragma: no cover - dep guaranteed by pyproject
            raise SamlError(f"SAML dependencies unavailable: {e}") from e

        # Parse with entity resolution + network access DISABLED to block XXE /
        # billion-laughs. (defusedxml.lxml is deprecated now that lxml exposes
        # these parser controls directly.)
        try:
            parser = etree.XMLParser(
                resolve_entities=False, no_network=True, dtd_validation=False,
                load_dtd=False, huge_tree=False)
            doc = etree.fromstring(raw, parser=parser)
        except Exception as e:
            raise SamlError(f"SAMLResponse XML is malformed or unsafe: {e}") from e
        # Reject any DOCTYPE (entity-expansion vector) outright.
        if doc.getroottree().docinfo.doctype:
            raise SamlError("SAMLResponse contains a DOCTYPE (rejected)")

        try:
            result = XMLVerifier().verify(doc, x509_cert=_pem(self._config.idp_x509_cert))
            return result.signed_xml
        except Exception as e:
            raise SamlError(f"SAML signature verification failed: {e}") from e

    def _check_conditions(self, signed_root) -> None:
        """Enforce audience restriction and NotOnOrAfter on the signed element."""
        # signed_root may be the Assertion or the Response; search within it.
        tag = signed_root.tag.split("}")[-1]
        assertion = signed_root if tag == "Assertion" else signed_root.find(
            ".//saml:Assertion", NS)
        if assertion is None:
            raise SamlError("No signed Assertion in SAMLResponse")

        conditions = assertion.find("saml:Conditions", NS)
        now = datetime.now(timezone.utc)
        if conditions is not None:
            not_after = conditions.get("NotOnOrAfter")
            if not_after and now >= _parse_instant(not_after):
                raise SamlError("SAML assertion has expired (NotOnOrAfter)")
            not_before = conditions.get("NotBefore")
            if not_before and now < _parse_instant(not_before):
                raise SamlError("SAML assertion not yet valid (NotBefore)")
            # Audience must match our SP entity id when the IdP restricts it.
            audiences = [a.text for a in conditions.findall(
                ".//saml:AudienceRestriction/saml:Audience", NS)]
            if audiences and self._config.sp_entity_id not in audiences:
                raise SamlError(
                    f"SAML audience mismatch: expected {self._config.sp_entity_id}")

    def _map_attributes(self, signed_root) -> RequestContext:
        tag = signed_root.tag.split("}")[-1]
        assertion = signed_root if tag == "Assertion" else signed_root.find(
            ".//saml:Assertion", NS)

        # NameID is the canonical subject identifier.
        name_id_el = assertion.find(".//saml:Subject/saml:NameID", NS)
        name_id = name_id_el.text if name_id_el is not None else ""

        attrs: dict[str, list[str]] = {}
        for attr in assertion.findall(".//saml:AttributeStatement/saml:Attribute", NS):
            key = attr.get("Name", "")
            values = [v.text or "" for v in attr.findall("saml:AttributeValue", NS)]
            attrs[key] = values

        m = self._config.attribute_mappings

        def first(mapped_key: str, default: str = "") -> str:
            vals = attrs.get(m.get(mapped_key, mapped_key), [])
            return vals[0] if vals else default

        roles = attrs.get(m.get("roles", "roles"), [])
        user_id = first("user_id") or name_id or "saml-user"
        return RequestContext(
            user_id=user_id,
            project_id=first("project_id"),
            roles=list(roles),
            scopes=[],
            auth_method=AuthMethod.SSO,
            email=first("email") or None,
        )

    # -- SP metadata ---------------------------------------------------------

    def sp_metadata(self) -> str:
        cfg = self._config
        return (
            f'<?xml version="1.0"?>'
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'entityID="{cfg.sp_entity_id}">'
            f'<md:SPSSODescriptor AuthnRequestsSigned="false" '
            f'WantAssertionsSigned="true" '
            f'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            f'<md:AssertionConsumerService '
            f'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{cfg.acs_url}" index="0" isDefault="true"/>'
            f'</md:SPSSODescriptor></md:EntityDescriptor>'
        )


def load_saml_config() -> SamlConfig:
    """Build a SamlConfig from AXON_SAML_* env vars. Empty → SSO disabled."""
    cert = os.environ.get("AXON_SAML_IDP_CERT", "")
    # Allow the cert to be provided as a file path.
    cert_path = os.environ.get("AXON_SAML_IDP_CERT_FILE", "")
    if cert_path and not cert:
        try:
            with open(cert_path, encoding="utf-8") as fh:
                cert = fh.read()
        except OSError:
            logger.warning("AXON_SAML_IDP_CERT_FILE %s not readable", cert_path)
    return SamlConfig(
        sp_entity_id=os.environ.get("AXON_SAML_SP_ENTITY_ID", ""),
        acs_url=os.environ.get("AXON_SAML_ACS_URL", ""),
        idp_entity_id=os.environ.get("AXON_SAML_IDP_ENTITY_ID", ""),
        idp_sso_url=os.environ.get("AXON_SAML_IDP_SSO_URL", ""),
        idp_x509_cert=cert,
    )


def _parse_instant(value: str) -> datetime:
    """Parse a SAML xsd:dateTime (…Z) into an aware UTC datetime."""
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
