import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import TokenVerifier, User, current_user


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


@pytest.fixture
def local_auth_app(monkeypatch):
    monkeypatch.delenv("DEV_AUTH_BYPASS", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    app = FastAPI()

    @app.get("/user")
    def user_identity(user: User = Depends(current_user)):
        return {"prefix": user.storage_prefix}

    return app


def test_local_bypass_is_disabled_by_default(local_auth_app):
    client = TestClient(local_auth_app, client=("127.0.0.1", 1234))
    assert client.get("/user").status_code == 401


@pytest.mark.parametrize("peer,base_url,origin", [
    ("127.0.0.1", "http://localhost", "http://localhost:4200"),
    ("::1", "http://[::1]", "http://[::1]:4200"),
])
def test_local_bypass_uses_a_stable_shared_identity(
    local_auth_app, monkeypatch, peer, base_url, origin
):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    client = TestClient(local_auth_app, base_url=base_url, client=(peer, 1234))
    response = client.get("/user", headers={"origin": origin})
    assert response.status_code == 200
    assert response.json() == {"prefix": User("local-development", "shared-user").storage_prefix}
    assert client.get("/user").json() == response.json()


@pytest.mark.parametrize("peer,base_url,origin,hosted", [
    ("192.0.2.1", "http://localhost", "http://localhost:4200", False),
    ("127.0.0.1", "https://public.example", "http://localhost:4200", False),
    ("127.0.0.1", "http://localhost", "https://public.example", False),
    ("127.0.0.1", "http://localhost", "null", False),
    ("127.0.0.1", "http://localhost", "http://localhost:4200", True),
])
def test_local_bypass_rejects_non_local_use(
    local_auth_app, monkeypatch, peer, base_url, origin, hosted
):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    if hosted:
        monkeypatch.setenv("WEBSITE_SITE_NAME", "hosted-dev")
    client = TestClient(local_auth_app, base_url=base_url, client=(peer, 1234))
    assert client.get("/user", headers={"origin": origin}).status_code == 403


def test_invalid_bypass_configuration_fails_explicitly(local_auth_app, monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "yes")
    client = TestClient(local_auth_app, client=("127.0.0.1", 1234))
    assert client.get("/user").status_code == 503
