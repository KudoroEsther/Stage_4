import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from uuid_utils import uuid7

from app.config import get_settings


settings = get_settings()


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_access_token(user: dict) -> str:
    now = utcnow()
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        "iat": now,
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.app_secret_key, algorithms=["HS256"])


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def derive_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def new_uuid() -> str:
    return str(uuid7())
