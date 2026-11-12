import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth import TokenVerifier


@pytest.fixture
def credentials():
    tenant, audience, user = (str(uuid4()) for _ in range(3))
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = TokenVerifier(tenant, audience)
    verifier.keys = Mock()
    verifier.keys.get_signing_key_from_jwt.return_value = SimpleNamespace(
        key=private_key.public_key()
    )
    now = int(time.time())
    claims = {
        "iss": verifier.issuer, "aud": audience, "tid": tenant, "oid": user,
        "iat": now, "nbf": now, "exp": now + 3600, "ver": "2.0", "scp": "access_as_user",
    }
    return verifier, private_key, claims


def test_valid_delegated_token_identifies_uploader(credentials):
    verifier, private_key, claims = credentials
    user = verifier.verify(jwt.encode(claims, private_key, algorithm="RS256"))
    assert user.object_id == claims["oid"]
    assert user.tenant_id == claims["tid"]


@pytest.mark.parametrize("changes,status", [
    ({"aud": str(uuid4())}, 401),
    ({"iss": "https://untrusted.example"}, 401),
    ({"tid": str(uuid4())}, 401),
    ({"exp": 0}, 401),
    ({"ver": "1.0"}, 401),
    ({"scp": ""}, 403),
    ({"oid": "not-a-user-id"}, 401),
])
def test_wrong_or_expired_tokens_are_rejected(credentials, changes, status):
    verifier, private_key, claims = credentials
    claims.update(changes)
    with pytest.raises(HTTPException) as error:
        verifier.verify(jwt.encode(claims, private_key, algorithm="RS256"))
    assert error.value.status_code == status


def test_invalid_signature_is_rejected(credentials):
    verifier, _, claims = credentials
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException) as error:
        verifier.verify(jwt.encode(claims, other_key, algorithm="RS256"))
    assert error.value.status_code == 401
