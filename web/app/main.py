import secrets
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def oauth_callback_url() -> str:
    return f"{settings.public_base_url.rstrip('/')}/auth/callback"


def is_logged_in(request: Request) -> bool:
    return bool(request.cookies.get("insighta_access_token") and request.cookies.get("insighta_refresh_token"))


def clear_session(response: RedirectResponse) -> None:
    for cookie_name in [
        "insighta_access_token",
        "insighta_refresh_token",
        "insighta_csrf",
        "insighta_oauth_state",
        "insighta_oauth_verifier",
    ]:
        response.delete_cookie(cookie_name)


def set_session(response: RedirectResponse, tokens: dict) -> None:
    response.set_cookie(
        "insighta_access_token",
        tokens["access_token"],
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=tokens.get("expires_in", 180),
    )
    response.set_cookie(
        "insighta_refresh_token",
        tokens["refresh_token"],
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=tokens.get("refresh_expires_in", 300),
    )
    response.set_cookie(
        "insighta_csrf",
        secrets.token_urlsafe(24),
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=tokens.get("refresh_expires_in", 300),
    )


def require_csrf(request: Request, csrf_token: str) -> None:
    cookie_token = request.cookies.get("insighta_csrf")
    if not cookie_token or cookie_token != csrf_token:
        raise ValueError("Invalid CSRF token")


async def backend_request(
    request: Request,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
):
    access_token = request.cookies.get("insighta_access_token")
    refresh_token = request.cookies.get("insighta_refresh_token")
    headers = {"X-API-Version": "1"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            f"{settings.backend_url}{path}",
            headers=headers,
            params=params,
            json=json,
            data=data,
            files=files,
        )
        if response.status_code != 401 or not refresh_token:
            return response, None

        refresh_response = await client.post(
            f"{settings.backend_url}/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        if refresh_response.status_code != 200:
            return response, None

        tokens = refresh_response.json()
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        retried = await client.request(
            method,
            f"{settings.backend_url}{path}",
            headers=headers,
            params=params,
            json=json,
            data=data,
            files=files,
        )
        return retried, tokens


async def backend_json(request: Request, method: str, path: str, *, params=None, json=None, data=None, files=None):
    response, tokens = await backend_request(
        request,
        method,
        path,
        params=params,
        json=json,
        data=data,
        files=files,
    )
    if not response.content:
        payload = {}
    else:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text or "Request failed"}
    return response, payload, tokens


@app.get("/")
async def home(request: Request):
    return RedirectResponse("/dashboard" if is_logged_in(request) else "/login", status_code=302)


@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/auth/login")
async def start_login(request: Request):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = __import__("base64").urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode("utf-8")).digest()
    ).decode("utf-8").rstrip("=")
    redirect_uri = oauth_callback_url()
    params = urlencode(
        {
            "client": "web",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
        }
    )
    response = RedirectResponse(f"{settings.backend_url}/auth/github?{params}", status_code=302)
    response.set_cookie(
        "insighta_oauth_state",
        state,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=300,
    )
    response.set_cookie(
        "insighta_oauth_verifier",
        verifier,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=300,
    )
    return response


@app.get("/auth/callback")
async def oauth_callback(request: Request, code: str, state: str):
    expected_state = request.cookies.get("insighta_oauth_state")
    verifier = request.cookies.get("insighta_oauth_verifier")
    if not expected_state or expected_state != state or not verifier:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid OAuth state. Please try again."},
            status_code=400,
        )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.backend_url}/auth/github/callback",
            json={
                "code": code,
                "state": state,
                "code_verifier": verifier,
                "redirect_uri": oauth_callback_url(),
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text or "Login failed."}
    if response.status_code != 200:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": payload.get("message", "Login failed.")},
            status_code=response.status_code,
        )

    redirect = RedirectResponse("/dashboard", status_code=302)
    clear_session(redirect)
    set_session(redirect, payload)
    return redirect


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    refresh_token = request.cookies.get("insighta_refresh_token")
    if refresh_token:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(
                f"{settings.backend_url}/auth/logout",
                json={"refresh_token": refresh_token},
            )
    response = RedirectResponse("/login", status_code=302)
    clear_session(response)
    return response


@app.get("/dashboard")
async def dashboard(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    response, payload, tokens = await backend_json(request, "GET", "/api/dashboard")
    if response.status_code >= 400:
        redirect = RedirectResponse("/login", status_code=302)
        clear_session(redirect)
        return redirect
    template_response = templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "data": payload["data"],
            "csrf_token": request.cookies.get("insighta_csrf"),
        },
    )
    if tokens:
        set_session(template_response, tokens)
    return template_response


@app.get("/profiles")
async def profiles_page(
    request: Request,
    gender: str | None = None,
    age_group: str | None = None,
    country_id: str | None = None,
    min_age: int | None = None,
    max_age: int | None = None,
    sort_by: str | None = None,
    order: str = "asc",
    page: int = 1,
    limit: int = 10,
):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    params = {
        "gender": gender,
        "age_group": age_group,
        "country_id": country_id,
        "min_age": min_age,
        "max_age": max_age,
        "sort_by": sort_by,
        "order": order,
        "page": page,
        "limit": limit,
    }
    params = {key: value for key, value in params.items() if value not in (None, "")}
    response, payload, tokens = await backend_json(request, "GET", "/api/profiles", params=params)
    if response.status_code >= 400:
        redirect = RedirectResponse("/login", status_code=302)
        clear_session(redirect)
        return redirect
    template_response = templates.TemplateResponse(
        "profiles.html",
        {
            "request": request,
            "payload": payload,
            "filters": params,
            "csrf_token": request.cookies.get("insighta_csrf"),
        },
    )
    if tokens:
        set_session(template_response, tokens)
    return template_response


@app.post("/profiles")
async def create_profile(request: Request, name: str = Form(...), csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    response, payload, tokens = await backend_json(
        request,
        "POST",
        "/api/profiles",
        json={"name": name},
    )
    target = "/profiles"
    if response.status_code >= 400:
        target = f"/profiles?error={payload.get('message', 'Unable to create profile')}"
    redirect = RedirectResponse(target, status_code=302)
    if tokens:
        set_session(redirect, tokens)
    return redirect


@app.post("/profiles/upload")
async def upload_profiles(
    request: Request,
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
):
    require_csrf(request, csrf_token)

    if not file.filename or not file.filename.lower().endswith(".csv"):
        return RedirectResponse("/profiles?error=CSV%20file%20required", status_code=302)

    await file.seek(0)
    files = {
        "file": (file.filename, file.file, file.content_type or "text/csv"),
    }
    response, payload, tokens = await backend_json(
        request,
        "POST",
        "/api/profiles/upload",
        files=files,
    )

    if response.status_code >= 400:
        redirect = RedirectResponse(
            f"/profiles?error={quote(payload.get('message', 'Unable to upload CSV'))}",
            status_code=302,
        )
    else:
        success_message = (
            f"Uploaded {payload.get('inserted', 0)} profiles "
            f"and skipped {payload.get('skipped', 0)} rows"
        )
        redirect = RedirectResponse(
            f"/profiles?success={quote(success_message)}",
            status_code=302,
        )

    if tokens:
        set_session(redirect, tokens)
    return redirect


@app.get("/profiles/{profile_id}")
async def profile_detail(request: Request, profile_id: str):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    response, payload, tokens = await backend_json(request, "GET", f"/api/profiles/{profile_id}")
    if response.status_code >= 400:
        return RedirectResponse("/profiles", status_code=302)
    template_response = templates.TemplateResponse(
        "profile_detail.html",
        {
            "request": request,
            "profile": payload["data"],
            "csrf_token": request.cookies.get("insighta_csrf"),
        },
    )
    if tokens:
        set_session(template_response, tokens)
    return template_response


@app.post("/profiles/{profile_id}/delete")
async def delete_profile(request: Request, profile_id: str, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    response, payload, tokens = await backend_json(request, "DELETE", f"/api/profiles/{profile_id}")
    redirect = RedirectResponse("/profiles", status_code=302)
    if tokens:
        set_session(redirect, tokens)
    return redirect


@app.get("/search")
async def search_page(request: Request, q: str | None = None, page: int = 1, limit: int = 10):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    payload = None
    tokens = None
    if q:
        _, payload, tokens = await backend_json(
            request,
            "GET",
            "/api/profiles/search",
            params={"q": q, "page": page, "limit": limit},
        )
    template_response = templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "payload": payload,
            "q": q,
            "csrf_token": request.cookies.get("insighta_csrf"),
        },
    )
    if tokens:
        set_session(template_response, tokens)
    return template_response


@app.get("/account")
async def account_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    response, payload, tokens = await backend_json(request, "GET", "/auth/me")
    if response.status_code >= 400:
        redirect = RedirectResponse("/login", status_code=302)
        clear_session(redirect)
        return redirect
    template_response = templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": payload["data"],
            "csrf_token": request.cookies.get("insighta_csrf"),
        },
    )
    if tokens:
        set_session(template_response, tokens)
    return template_response
