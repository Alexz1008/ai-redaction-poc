import hashlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    tenant_id: str
    object_id: str

    @property
    def storage_prefix(self) -> str:
        return hashlib.sha256(f"{self.tenant_id}:{self.object_id}".encode()).hexdigest()


class TokenVerifier:
    def __init__(self, tenant_id: str, audience: str):
        self.tenant_id = str(UUID(tenant_id))
        self.audience = str(UUID(audience))
        self.issuer = f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        self.keys = PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys",
            timeout=10,
        )

    def verify(self, token: str) -> User:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid"]},
                leeway=30,
            )
        except PyJWKClientConnectionError:
            logger.warning("Entra signing keys are unavailable.")
            raise HTTPException(503, "Sign-in verification is temporarily unavailable.") from None
        except (InvalidTokenError, PyJWKClientError):
            raise HTTPException(
                401, "Invalid or expired access token.", headers={"WWW-Authenticate": "Bearer"}
            ) from None

        if claims["tid"] != self.tenant_id or claims.get("ver") != "2.0":
            raise HTTPException(401, "An access token from the configured tenant is required.")
        if "access_as_user" not in claims.get("scp", "").split():
            raise HTTPException(403, "The access_as_user delegated permission is required.")
        try:
            object_id = str(UUID(claims["oid"]))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(401, "The access token has no valid user identity.") from None
        return User(self.tenant_id, object_id)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    tenant = os.environ.get("ENTRA_TENANT_ID", "")
    audience = os.environ.get("ENTRA_API_CLIENT_ID", "")
    try:
        return TokenVerifier(tenant, audience)
    except ValueError:
        logger.error("ENTRA_TENANT_ID and ENTRA_API_CLIENT_ID must be configured as UUIDs.")
        raise HTTPException(503, "Backend sign-in settings are not configured.") from None


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    if credentials is None:
        raise HTTPException(
            401, "Sign-in is required.", headers={"WWW-Authenticate": "Bearer"}
        )
    return get_token_verifier().verify(credentials.credentials)
