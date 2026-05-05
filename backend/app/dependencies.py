import time
from collections import defaultdict, deque
from math import ceil
from urllib.parse import urlencode

import jwt
from fastapi import Depends, Header, HTTPException, Request

from app.config import get_settings
from app.db import database
from app.models import refresh_tokens, users
from app.security import decode_access_token, hash_token, utcnow


settings = get_settings()
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


def error(message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"status": "error", "message": message})


async def require_api_version(x_api_version: str | None = Header(default=None)) -> str:
    if x_api_version != "1":
        raise error("API version header required", 400)
    return x_api_version


def _enforce_rate_limit(key: str, limit: int) -> None:
    now = time.time()
    window = _rate_windows[key]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= limit:
        raise error("Rate limit exceeded", 429)
    window.append(now)


async def auth_rate_limit(request: Request) -> None:
    key = f"auth:{request.client.host if request.client else 'unknown'}"
    _enforce_rate_limit(key, settings.auth_rate_limit_per_minute)


async def get_current_user(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        raise error("Authentication required", 401)

    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise error("Invalid or expired access token", 401)

    user = await database.fetch_one(users.select().where(users.c.id == payload["sub"]))
    if not user:
        raise error("User not found", 401)
    if not user["is_active"]:
        raise error("User is inactive", 403)

    _enforce_rate_limit(f"api:{user['id']}", settings.api_rate_limit_per_minute)
    return dict(user)


def require_role(*allowed_roles: str):
    async def dependency(user=Depends(get_current_user)):
        if user["role"] not in allowed_roles:
            raise error("Forbidden", 403)
        return user

    return dependency


def pagination_payload(*, request: Request, page: int, limit: int, total: int) -> dict:
    total_pages = max(1, ceil(total / limit)) if total else 0
    params = dict(request.query_params)

    def make_link(target_page: int | None):
        if target_page is None or target_page < 1 or (total_pages and target_page > total_pages):
            return None
        params["page"] = str(target_page)
        params["limit"] = str(limit)
        query = urlencode(params)
        return f"{request.url.path}?{query}"

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "links": {
            "self": make_link(page),
            "next": make_link(page + 1) if total_pages and page < total_pages else None,
            "prev": make_link(page - 1) if page > 1 else None,
        },
    }


async def get_refresh_token_record(token: str):
    token_hash = hash_token(token)
    record = await database.fetch_one(
        refresh_tokens.select().where(refresh_tokens.c.token_hash == token_hash)
    )
    if not record or record["revoked_at"] is not None or record["expires_at"] <= utcnow():
        raise error("Invalid refresh token", 401)
    return dict(record)
