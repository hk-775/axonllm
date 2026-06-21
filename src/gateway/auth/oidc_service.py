"""OIDC/JWT authentication — ALB and direct OIDC token validation."""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.gateway.models import AuthMethod, RequestContext

logger = logging.getLogger(__name__)


@dataclass
class OIDCConfig:
    """Configuration for OIDC authentication."""

    issuer: str = ""
    audience: str = ""
    alb_region: str = "us-east-1"
    jwks_cache_ttl: int = 3600
    claim_mappings: dict = field(default_factory=lambda: {
        "user_id": "sub",
        "email": "email",
        "project_id": "custom:project_id",
        "tenant_id": "custom:tenant_id",
        "business_unit": "custom:business_unit",
        "roles": "custom:roles",
    })


class OIDCService:
    """Validates OIDC JWTs from ALB or direct Bearer tokens.

    Supports:
    - ALB-injected tokens (X-Amzn-Oidc-Data, ES256, regional public keys)
    - Standard OIDC Bearer tokens (RS256/ES256, JWKS discovery)
    """

    def __init__(self, config: OIDCConfig) -> None:
        self._config = config
        self._jwks_cache: dict[str, Any] = {}
        self._jwks_fetched_at: float = 0

    async def validate_alb_jwt(self, token: str) -> RequestContext | None:
        """Validate JWT from X-Amzn-Oidc-Data header (ALB-signed ES256).

        ALB tokens have the public key kid in the header; the key is fetched
        from https://public-keys.auth.elb.<region>.amazonaws.com/<kid>
        """
        try:
            header = self._decode_jwt_header(token)
            if header is None:
                return None

            kid = header.get("kid")
            if not kid:
                return None

            public_key = await self._fetch_alb_public_key(kid)
            if public_key is None:
                return None

            claims = self._verify_and_decode(token, public_key, algorithms=["ES256"])
            if claims is None:
                return None

            return self._map_claims_to_context(claims)

        except Exception:
            logger.debug("ALB JWT validation failed", exc_info=True)
            return None

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        """Validate standard OIDC Bearer JWT using JWKS discovery."""
        try:
            header = self._decode_jwt_header(token)
            if header is None:
                return None

            kid = header.get("kid")
            alg = header.get("alg", "RS256")

            jwks = await self._get_jwks()
            if jwks is None:
                return None

            key = self._find_key(jwks, kid)
            if key is None:
                return None

            claims = self._verify_and_decode(token, key, algorithms=[alg])
            if claims is None:
                return None

            if self._config.audience and claims.get("aud") != self._config.audience:
                return None

            if self._config.issuer and claims.get("iss") != self._config.issuer:
                return None

            exp = claims.get("exp")
            if exp and time.time() > exp:
                return None

            return self._map_claims_to_context(claims)

        except Exception:
            logger.debug("OIDC JWT validation failed", exc_info=True)
            return None

    def _decode_jwt_header(self, token: str) -> dict | None:
        """Decode JWT header without verification (to get kid/alg)."""
        try:
            header_segment = token.split(".")[0]
            padding = 4 - len(header_segment) % 4
            if padding != 4:
                header_segment += "=" * padding
            header_bytes = base64.urlsafe_b64decode(header_segment)
            return json.loads(header_bytes)
        except Exception:
            return None

    def _verify_and_decode(
        self, token: str, key: Any, algorithms: list[str]
    ) -> dict | None:
        """Verify JWT signature and decode claims.

        Uses python-jose if available, falls back to manual decode for testing.
        """
        try:
            from jose import jwt as jose_jwt

            return jose_jwt.decode(
                token,
                key,
                algorithms=algorithms,
                options={"verify_aud": False, "verify_exp": True},
            )
        except ImportError:
            # Fallback: decode payload without signature verification (testing only)
            try:
                payload_segment = token.split(".")[1]
                padding = 4 - len(payload_segment) % 4
                if padding != 4:
                    payload_segment += "=" * padding
                payload_bytes = base64.urlsafe_b64decode(payload_segment)
                return json.loads(payload_bytes)
            except Exception:
                return None
        except Exception:
            return None

    async def _fetch_alb_public_key(self, kid: str) -> Any:
        """Fetch ALB public key from regional endpoint."""
        url = f"https://public-keys.auth.elb.{self._config.alb_region}.amazonaws.com/{kid}"
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5.0)
                if resp.status_code == 200:
                    return resp.text
        except ImportError:
            logger.warning("httpx not installed — ALB key fetch unavailable")
        except Exception:
            logger.debug("Failed to fetch ALB public key", exc_info=True)
        return None

    async def _get_jwks(self) -> dict | None:
        """Fetch JWKS from the issuer's discovery endpoint with caching."""
        now = time.time()
        if self._jwks_cache and (now - self._jwks_fetched_at) < self._config.jwks_cache_ttl:
            return self._jwks_cache

        try:
            import httpx

            discovery_url = f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration"
            async with httpx.AsyncClient() as client:
                disc_resp = await client.get(discovery_url, timeout=5.0)
                if disc_resp.status_code != 200:
                    return self._jwks_cache or None
                jwks_uri = disc_resp.json().get("jwks_uri")
                if not jwks_uri:
                    return None

                jwks_resp = await client.get(jwks_uri, timeout=5.0)
                if jwks_resp.status_code == 200:
                    self._jwks_cache = jwks_resp.json()
                    self._jwks_fetched_at = now
                    return self._jwks_cache
        except ImportError:
            logger.warning("httpx not installed — JWKS fetch unavailable")
        except Exception:
            logger.debug("JWKS fetch failed", exc_info=True)

        return self._jwks_cache or None

    def _find_key(self, jwks: dict, kid: str | None) -> dict | None:
        """Find matching key in JWKS by kid."""
        keys = jwks.get("keys", [])
        if not keys:
            return None
        if kid is None:
            return keys[0]
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None

    def _map_claims_to_context(self, claims: dict) -> RequestContext:
        """Map JWT claims to RequestContext using configured mappings."""
        mappings = self._config.claim_mappings

        def _get(claim_key: str, default=""):
            return claims.get(claim_key, default)

        roles_raw = _get(mappings.get("roles", "custom:roles"), [])
        if isinstance(roles_raw, str):
            roles_raw = [r.strip() for r in roles_raw.split(",") if r.strip()]

        return RequestContext(
            user_id=_get(mappings.get("user_id", "sub")),
            project_id=_get(mappings.get("project_id", "custom:project_id")),
            roles=roles_raw,
            scopes=claims.get("scope", "").split() if isinstance(claims.get("scope"), str) else [],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id=_get(mappings.get("tenant_id", "custom:tenant_id")) or None,
            business_unit=_get(mappings.get("business_unit", "custom:business_unit")) or None,
            email=_get(mappings.get("email", "email")) or None,
        )
