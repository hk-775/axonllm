"""Exact-key AWS KMS routing snapshot signing."""

from __future__ import annotations

import pytest

from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_signing import (
    KmsRoutingConfigAuthenticator,
    RoutingConfigSignatureError,
)


KEY_ARN = (
    "arn:aws:kms:us-west-2:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)
CONFIG = {
    "models": [
        {
            "name": "test",
            "description": "test",
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "test",
                }
            ],
        }
    ]
}


class _Kms:
    def __init__(self) -> None:
        self.sign_request: dict | None = None
        self.verify_request: dict | None = None
        self.signature_valid = True
        self.response_key = KEY_ARN

    def sign(self, **kwargs) -> dict:
        self.sign_request = kwargs
        return {
            "KeyId": self.response_key,
            "SigningAlgorithm": "ECDSA_SHA_256",
            "Signature": b"der-signature",
        }

    def verify(self, **kwargs) -> dict:
        self.verify_request = kwargs
        return {
            "KeyId": self.response_key,
            "SigningAlgorithm": "ECDSA_SHA_256",
            "SignatureValid": self.signature_valid,
        }


async def test_kms_sign_and_verify_use_the_exact_digest_contract() -> None:
    client = _Kms()
    authenticator = KmsRoutingConfigAuthenticator(
        KEY_ARN,
        region="us-west-2",
        client=client,
    )
    unsigned = RoutingConfigSnapshot.from_config(CONFIG, revision=3)

    signed = await authenticator.sign(unsigned)
    await authenticator.verify(signed)

    assert client.sign_request == {
        "KeyId": KEY_ARN,
        "Message": unsigned.signing_digest,
        "MessageType": "DIGEST",
        "SigningAlgorithm": "ECDSA_SHA_256",
    }
    assert client.verify_request == {
        "KeyId": KEY_ARN,
        "Message": signed.signing_digest,
        "MessageType": "DIGEST",
        "Signature": b"der-signature",
        "SigningAlgorithm": "ECDSA_SHA_256",
    }


async def test_kms_verification_rejects_invalid_or_wrong_key_results() -> None:
    client = _Kms()
    authenticator = KmsRoutingConfigAuthenticator(
        KEY_ARN,
        region="us-west-2",
        client=client,
    )
    signed = await authenticator.sign(
        RoutingConfigSnapshot.from_config(CONFIG, revision=1)
    )

    client.signature_valid = False
    with pytest.raises(RoutingConfigSignatureError, match="invalid"):
        await authenticator.verify(signed)

    client.signature_valid = True
    client.response_key = (
        "arn:aws:kms:us-west-2:123456789012:"
        "key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(RoutingConfigSignatureError, match="unexpected"):
        await authenticator.verify(signed)


def test_kms_authenticator_requires_a_full_key_arn() -> None:
    with pytest.raises(ValueError, match="full KMS key ARN"):
        KmsRoutingConfigAuthenticator(
            "alias/axonllm-routing",
            region="us-west-2",
            client=_Kms(),
        )
    with pytest.raises(ValueError, match="runtime region"):
        KmsRoutingConfigAuthenticator(
            KEY_ARN,
            region="us-east-1",
            client=_Kms(),
        )
