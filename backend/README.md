# Insighta Labs+ Backend

Shared backend for the CLI and web portal. It preserves the Stage 2 profile intelligence behavior and adds GitHub OAuth with PKCE, short-lived access and refresh tokens, RBAC, API versioning, CSV export, rate limiting, and request logging.

## Run

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload
python seed.py
```

## Key endpoints

- `GET /auth/github`
- `POST /auth/github/callback`
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /auth/me`
- `GET /api/profiles`
- `GET /api/profiles/search`
- `GET /api/profiles/export?format=csv`
- `POST /api/profiles`
- `DELETE /api/profiles/{id}`

All profile endpoints require `X-API-Version: 1`.
