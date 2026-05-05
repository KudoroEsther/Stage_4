from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import jwt
import sqlalchemy
from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cache import query_cache
from app.config import get_settings
from app.db import database, prepare_database
from app.dependencies import (
    auth_rate_limit,
    error,
    get_current_user,
    get_refresh_token_record,
    pagination_payload,
    require_api_version,
    require_role,
)
from app.models import profiles, users
from app.nlp import parse_query
from app.query_utils import build_profiles_cache_key, normalize_profile_filters
from app.schemas import (
    AuthExchangeRequest,
    LogoutRequest,
    ProfileRequest,
    RefreshRequest,
    UpdateRoleRequest,
)
from app.security import derive_code_challenge, hash_token, utcnow
from app.services import (
    apply_filters,
    apply_sorting,
    build_github_oauth_url,
    consume_oauth_state,
    create_profile_from_name,
    exchange_github_code,
    get_oauth_state,
    insert_profile,
    issue_token_pair,
    paginate,
    profile_dict,
    render_csv,
    revoke_refresh_token,
    rotate_refresh_token,
    store_oauth_state,
    get_test_auth_role,
    upsert_user,
    upsert_test_user,
)
from app.uploads import process_csv_upload


settings = get_settings()


async def invalidate_profile_query_cache(*, profile_id: str | None = None) -> None:
    """
    Broad cache invalidation keeps write handling simple and predictable.
    That matches the design goal of reducing read load without adding
    complicated cache dependency tracking.
    """

    await query_cache.invalidate_prefix("profiles:list:")
    await query_cache.invalidate_prefix("profiles:search:")
    await query_cache.invalidate_prefix("dashboard:summary:")
    if profile_id:
        await query_cache.invalidate(f"profiles:detail:{profile_id}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    prepare_database()
    await database.connect()
    yield
    await database.disconnect()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"https?://.*",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = datetime.now(UTC)
    response = await call_next(request)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    print(
        f"{request.method} {request.url.path} "
        f"status={response.status_code} duration_ms={duration_ms}"
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    message = first.get("msg", "Invalid input").replace("Value error, ", "")
    message_lower = message.lower()
    status_code = 400 if (
        "empty" in message_lower
        or "missing" in message_lower
        or "required" in message_lower
    ) else 422
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "message": message},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": message},
    )


@app.exception_handler(jwt.PyJWTError)
async def jwt_exception_handler(request: Request, exc: jwt.PyJWTError):
    return JSONResponse(
        status_code=401,
        content={"status": "error", "message": "Invalid or expired access token"},
    )


@app.exception_handler(httpx.HTTPError)
async def httpx_exception_handler(request: Request, exc: httpx.HTTPError):
    return JSONResponse(
        status_code=502,
        content={"status": "error", "message": "Upstream service failure"},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=502,
        content={"status": "error", "message": str(exc)},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if hasattr(exc, "detail") and isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    raise exc


@app.get("/health")
async def health():
    return {"status": "success", "message": "ok"}


@app.get("/auth/github")
async def github_auth_start(
    client: str = Query(default="web", pattern="^(cli|web|browser_direct)$"),
    redirect_uri: str | None = None,
    state: str | None = None,
    code_challenge: str | None = None,
    return_to: str | None = None,
    _: None = Depends(auth_rate_limit),
):
    client_id, client_secret = settings.github_credentials_for(client)
    if not client_id or not client_secret:
        raise error(f"GitHub OAuth is not configured for client type '{client}'", 500)

    callback_uri = redirect_uri or "http://localhost:8000/auth/github/callback"
    stored_state, _, challenge = await store_oauth_state(
        client_type=client,
        redirect_uri=callback_uri,
        return_to=return_to,
        state=state,
        code_challenge=code_challenge,
    )
    oauth_url = build_github_oauth_url(
        client_id=client_id,
        redirect_uri=callback_uri,
        state=stored_state,
        code_challenge=challenge,
    )
    return RedirectResponse(oauth_url, status_code=302)


@app.get("/auth/github/callback")
async def github_auth_callback_get(
    code: str | None = None,
    state: str | None = None,
    _: None = Depends(auth_rate_limit),
):
    if not code:
        raise error("code is required", 400)
    if not state:
        raise error("state is required", 400)

    oauth_state = await get_oauth_state(state)
    if not oauth_state:
        raise error("Invalid or expired OAuth state", 400)
    if not oauth_state.get("code_verifier"):
        raise error("External callback clients must exchange code with POST /auth/github/callback", 400)

    github_user = await exchange_github_code(
        client_type=oauth_state["client_type"],
        code=code,
        redirect_uri=oauth_state["redirect_uri"],
        code_verifier=oauth_state["code_verifier"],
    )
    user = await upsert_user(github_user)
    await issue_token_pair(user)
    await consume_oauth_state(state)

    html = f"""
    <html>
      <body>
        <h1>Insighta Labs+ Login Complete</h1>
        <p>Logged in as @{user["username"]}</p>
        <p>You can return to the client application.</p>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/auth/github/callback")
async def github_auth_callback_post(
    payload: AuthExchangeRequest,
    _: None = Depends(auth_rate_limit),
):
    if not payload.code:
        raise error("code is required", 400)
    if not payload.state:
        raise error("state is required", 400)

    if settings.enable_test_auth and payload.code.startswith("test_code"):
        role = get_test_auth_role(code=payload.code, state=payload.state)
        user = await upsert_test_user(role)
        tokens = await issue_token_pair(user)
        return {
            "status": "success",
            **tokens,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "role": user["role"],
                "avatar_url": user["avatar_url"],
                "is_active": user["is_active"],
            },
        }

    oauth_state = await get_oauth_state(payload.state)
    if not oauth_state:
        raise error("Invalid or expired OAuth state", 400)

    verifier = payload.code_verifier or oauth_state.get("code_verifier")
    if not verifier:
        raise error("code_verifier is required", 400)
    if oauth_state.get("code_challenge") != derive_code_challenge(verifier):
        raise error("Invalid PKCE code verifier", 400)

    redirect_uri = payload.redirect_uri or oauth_state["redirect_uri"]
    github_user = await exchange_github_code(
        client_type=oauth_state["client_type"],
        code=payload.code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    user = await upsert_user(github_user)
    tokens = await issue_token_pair(user)
    await consume_oauth_state(payload.state)
    return {
        "status": "success",
        **tokens,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "avatar_url": user["avatar_url"],
            "is_active": user["is_active"],
        },
    }


@app.post("/auth/refresh")
async def refresh_auth_token(payload: RefreshRequest, _: None = Depends(auth_rate_limit)):
    if not payload.refresh_token:
        raise error("refresh_token is required", 400)
    record = await get_refresh_token_record(payload.refresh_token)
    tokens = await rotate_refresh_token(record)
    return {"status": "success", **tokens}


@app.post("/auth/logout")
async def logout(payload: LogoutRequest, _: None = Depends(auth_rate_limit)):
    if not payload.refresh_token:
        raise error("refresh_token is required", 400)
    await revoke_refresh_token(hash_token(payload.refresh_token))
    return {"status": "success", "message": "Logged out"}


@app.get("/auth/me")
async def who_am_i(user=Depends(get_current_user)):
    return {
        "status": "success",
        "data": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "avatar_url": user["avatar_url"],
            "is_active": user["is_active"],
            "last_login_at": user["last_login_at"].isoformat() if user["last_login_at"] else None,
            "created_at": user["created_at"].isoformat(),
        },
    }


@app.get("/api/users/me", dependencies=[Depends(require_api_version)])
async def current_api_user(user=Depends(require_role("admin", "analyst"))):
    return {
        "status": "success",
        "data": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "avatar_url": user["avatar_url"],
            "is_active": user["is_active"],
            "last_login_at": user["last_login_at"].isoformat() if user["last_login_at"] else None,
            "created_at": user["created_at"].isoformat(),
        },
    }


@app.get("/api/dashboard", dependencies=[Depends(require_api_version)])
async def dashboard_metrics(user=Depends(require_role("admin", "analyst"))):
    cache_key = f"dashboard:summary:{user['role']}"
    cached = await query_cache.get(cache_key)
    if cached is not None:
        return cached

    total_profiles = await database.fetch_val(
        sqlalchemy.select(sqlalchemy.func.count()).select_from(profiles)
    )
    gender_rows = await database.fetch_all(
        sqlalchemy.select(profiles.c.gender, sqlalchemy.func.count().label("count"))
        .group_by(profiles.c.gender)
    )
    response = {
        "status": "success",
        "data": {
            "total_profiles": total_profiles,
            "by_gender": {row["gender"]: row["count"] for row in gender_rows},
            "current_user_role": user["role"],
        },
    }
    await query_cache.set(
        cache_key,
        response,
        settings.dashboard_cache_ttl_seconds,
    )
    return response


@app.get("/api/profiles", dependencies=[Depends(require_api_version)])
async def list_profiles(
    request: Request,
    gender: str | None = Query(default=None),
    age_group: str | None = Query(default=None),
    country_id: str | None = Query(default=None),
    min_age: int | None = Query(default=None),
    max_age: int | None = Query(default=None),
    min_gender_probability: float | None = Query(default=None),
    min_country_probability: float | None = Query(default=None),
    sort_by: str | None = Query(default=None, pattern="^(age|created_at|gender_probability)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=50),
    user=Depends(require_role("admin", "analyst")),
):
    normalized_filters = normalize_profile_filters(
        {
            "gender": gender,
            "age_group": age_group,
            "country_id": country_id,
            "min_age": min_age,
            "max_age": max_age,
            "min_gender_probability": min_gender_probability,
            "min_country_probability": min_country_probability,
        }
    )
    cache_key = build_profiles_cache_key(
        namespace="profiles:list",
        filters=normalized_filters,
        page=page,
        limit=limit,
        sort_by=sort_by,
        order=order,
    )
    cached = await query_cache.get(cache_key)
    if cached is not None:
        return cached

    base_query = profiles.select()
    base_query = apply_filters(
        base_query,
        normalized_filters.get("gender"),
        normalized_filters.get("age_group"),
        normalized_filters.get("country_id"),
        normalized_filters.get("min_age"),
        normalized_filters.get("max_age"),
        normalized_filters.get("min_gender_probability"),
        normalized_filters.get("min_country_probability"),
    )
    base_query = apply_sorting(base_query, sort_by, order)
    total = await database.fetch_val(
        sqlalchemy.select(sqlalchemy.func.count()).select_from(base_query.alias("sub"))
    )
    rows = await database.fetch_all(paginate(base_query, page, limit))
    payload = pagination_payload(request=request, page=page, limit=limit, total=total)
    response = {"status": "success", **payload, "data": [profile_dict(row) for row in rows]}
    await query_cache.set(cache_key, response, settings.query_cache_ttl_seconds)
    return response


@app.get("/api/profiles/search", dependencies=[Depends(require_api_version)])
async def search_profiles(
    request: Request,
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=50),
    user=Depends(require_role("admin", "analyst")),
):
    if not q or not q.strip():
        raise error("Missing or empty query", 400)

    filters = parse_query(q)
    if filters is None:
        raise error("Unable to interpret query", 400)

    normalized_filters = normalize_profile_filters(filters)
    cache_key = build_profiles_cache_key(
        namespace="profiles:search",
        filters=normalized_filters,
        page=page,
        limit=limit,
    )
    cached = await query_cache.get(cache_key)
    if cached is not None:
        return cached

    base_query = apply_filters(
        profiles.select(),
        normalized_filters.get("gender"),
        normalized_filters.get("age_group"),
        normalized_filters.get("country_id"),
        normalized_filters.get("min_age"),
        normalized_filters.get("max_age"),
        None,
        None,
    )
    total = await database.fetch_val(
        sqlalchemy.select(sqlalchemy.func.count()).select_from(base_query.alias("sub"))
    )
    rows = await database.fetch_all(paginate(base_query, page, limit))
    payload = pagination_payload(request=request, page=page, limit=limit, total=total)
    response = {"status": "success", **payload, "data": [profile_dict(row) for row in rows]}
    await query_cache.set(cache_key, response, settings.query_cache_ttl_seconds)
    return response


@app.get("/api/profiles/export", dependencies=[Depends(require_api_version)])
async def export_profiles(
    request: Request,
    format: str = Query(default="csv", pattern="^csv$"),
    gender: str | None = Query(default=None),
    age_group: str | None = Query(default=None),
    country_id: str | None = Query(default=None),
    min_age: int | None = Query(default=None),
    max_age: int | None = Query(default=None),
    min_gender_probability: float | None = Query(default=None),
    min_country_probability: float | None = Query(default=None),
    sort_by: str | None = Query(default=None, pattern="^(age|created_at|gender_probability)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    user=Depends(require_role("admin", "analyst")),
):
    _ = request
    normalized_filters = normalize_profile_filters(
        {
            "gender": gender,
            "age_group": age_group,
            "country_id": country_id,
            "min_age": min_age,
            "max_age": max_age,
            "min_gender_probability": min_gender_probability,
            "min_country_probability": min_country_probability,
        }
    )
    query = apply_filters(
        profiles.select(),
        normalized_filters.get("gender"),
        normalized_filters.get("age_group"),
        normalized_filters.get("country_id"),
        normalized_filters.get("min_age"),
        normalized_filters.get("max_age"),
        normalized_filters.get("min_gender_probability"),
        normalized_filters.get("min_country_probability"),
    )
    query = apply_sorting(query, sort_by, order)
    rows = [profile_dict(row) for row in await database.fetch_all(query)]
    timestamp = utcnow().strftime("%Y%m%d%H%M%S")
    csv_body = render_csv(rows)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="profiles_{timestamp}.csv"',
        },
    )


@app.get("/api/profiles/{profile_id}", dependencies=[Depends(require_api_version)])
async def get_profile(profile_id: str, user=Depends(require_role("admin", "analyst"))):
    cache_key = f"profiles:detail:{profile_id}"
    cached = await query_cache.get(cache_key)
    if cached is not None:
        return cached

    row = await database.fetch_one(profiles.select().where(profiles.c.id == profile_id))
    if not row:
        raise error("Profile not found", 404)
    response = {"status": "success", "data": profile_dict(row)}
    await query_cache.set(
        cache_key,
        response,
        settings.profile_detail_cache_ttl_seconds,
    )
    return response


@app.post("/api/profiles", dependencies=[Depends(require_api_version)])
async def create_profile(payload: ProfileRequest, user=Depends(require_role("admin"))):
    profile, existed = await insert_profile(payload.name)
    response = {"status": "success", "data": profile}
    await invalidate_profile_query_cache(profile_id=profile["id"])
    if existed:
        response["message"] = "Profile already exists"
        return JSONResponse(status_code=200, content=response)
    return JSONResponse(status_code=201, content=response)


@app.delete("/api/profiles/{profile_id}", dependencies=[Depends(require_api_version)])
async def delete_profile(profile_id: str, user=Depends(require_role("admin"))):
    row = await database.fetch_one(profiles.select().where(profiles.c.id == profile_id))
    if not row:
        raise error("Profile not found", 404)
    await database.execute(profiles.delete().where(profiles.c.id == profile_id))
    await invalidate_profile_query_cache(profile_id=profile_id)
    return Response(status_code=204)


@app.post("/api/profiles/upload", dependencies=[Depends(require_api_version)])
async def upload_profiles_csv(
    file: UploadFile = File(...),
    user=Depends(require_role("admin")),
):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise error("CSV file required", 400)

    summary = await process_csv_upload(file)
    await invalidate_profile_query_cache()
    return summary


@app.get("/api/admin/users", dependencies=[Depends(require_api_version)])
async def list_users(user=Depends(require_role("admin"))):
    rows = await database.fetch_all(users.select().order_by(users.c.created_at.asc()))
    return {"status": "success", "data": [dict(row) for row in rows]}


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(require_api_version)])
async def update_user_role(
    user_id: str,
    payload: UpdateRoleRequest,
    admin=Depends(require_role("admin")),
):
    row = await database.fetch_one(users.select().where(users.c.id == user_id))
    if not row:
        raise error("User not found", 404)
    await database.execute(
        users.update()
        .where(users.c.id == user_id)
        .values(role=payload.role, is_active=payload.is_active)
    )
    await query_cache.invalidate_prefix("dashboard:summary:")
    updated = await database.fetch_one(users.select().where(users.c.id == user_id))
    return {"status": "success", "data": dict(updated)}
