# Insighta Labs+ Stage 3

Insighta Labs+ Stage 3 is a multi-interface profile intelligence system with a shared backend, a server-rendered web portal, and a locally installed CLI. The backend is the source of truth for authentication, token lifecycle, user roles, profile data, exports, rate limiting, and API versioning.

This repository also keeps the original Stage 2 files at the root, but the runnable Stage 3 system lives in:

- `backend/` for the shared FastAPI API
- `web/` for the FastAPI web portal
- `cli/` for the installable `insighta` command-line client

## System Overview

The three applications work together:

- `backend/` handles GitHub OAuth with PKCE, JWT access tokens, opaque refresh tokens, RBAC, request logging, rate limiting, and profile APIs
- `web/` authenticates users through the backend, stores tokens in HTTP-only cookies, and adds CSRF protection for browser form actions
- `cli/` launches a local OAuth callback server, exchanges the GitHub code with the backend, stores tokens in `~/.insighta/credentials.json`, and auto-refreshes sessions when possible

## Core Features

- GitHub OAuth with PKCE for both browser and CLI clients
- Short-lived access tokens and rotating refresh tokens
- Role-based access control for `admin` and `analyst`
- Versioned API access using `X-API-Version: 1`
- Filtering, sorting, pagination, search, and CSV export for profiles
- Auth and API rate limiting
- Shared backend used by both the web portal and CLI

## Repository Structure

```text
.
|-- backend/
|   |-- app/
|   |-- tests/
|   |-- .env.example
|   `-- requirements.txt
|-- web/
|   |-- app/
|   |-- .env.example
|   `-- requirements.txt
|-- cli/
|   |-- insighta_cli/
|   `-- pyproject.toml
|-- main.py
|-- database.py
`-- README.md
```

## Architecture

### Authentication Flow

1. The client generates a `state`, `code_verifier`, and `code_challenge`.
2. The client opens `GET /auth/github` on the backend.
3. GitHub redirects back to the client callback URL.
4. The client validates `state` and sends `code` and `code_verifier` to `POST /auth/github/callback`.
5. The backend exchanges the code with GitHub, upserts the user, and issues:
   - an access token
   - a refresh token
6. The CLI stores both tokens locally.
7. The web portal stores both tokens in HTTP-only cookies.

### Token Lifecycle

- Access token expiry: `180` seconds by default
- Refresh token expiry: `300` seconds by default
- Refresh tokens are stored server-side as SHA-256 hashes
- `POST /auth/refresh` rotates the refresh token on every use
- `POST /auth/logout` revokes the refresh token server-side

### Role Enforcement

- `admin` can view, search, export, create, delete, and manage users
- `analyst` has read-only access to dashboard, search, list, detail, export, and account endpoints
- All protected `/api/*` routes require authentication
- Versioned routes require `X-API-Version: 1`

## Prerequisites

Before running the system locally, make sure you have:

- Python `3.11+`
- `pip`
- A PostgreSQL database, or a local SQLite database if you choose to override the default setup
- GitHub OAuth application credentials

## Environment Variables

### Backend

Create `backend/.env` from `backend/.env.example`.

Important variables:

- `DATABASE_URL`
- `APP_SECRET_KEY`
- `GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET`
- `GITHUB_WEB_CLIENT_ID`
- `GITHUB_WEB_CLIENT_SECRET`
- `GITHUB_CLI_CLIENT_ID`
- `GITHUB_CLI_CLIENT_SECRET`
- `CORS_ORIGINS`
- `ACCESS_TOKEN_TTL_SECONDS`
- `REFRESH_TOKEN_TTL_SECONDS`
- `AUTH_RATE_LIMIT_PER_MINUTE`
- `API_RATE_LIMIT_PER_MINUTE`

Default example:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/insighta
APP_SECRET_KEY=change-me
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret
GITHUB_WEB_CLIENT_ID=your-web-oauth-client-id
GITHUB_WEB_CLIENT_SECRET=your-web-oauth-client-secret
GITHUB_CLI_CLIENT_ID=your-cli-oauth-client-id
GITHUB_CLI_CLIENT_SECRET=your-cli-oauth-client-secret
CORS_ORIGINS=http://localhost:8000,http://localhost:3000
ACCESS_TOKEN_TTL_SECONDS=180
REFRESH_TOKEN_TTL_SECONDS=300
AUTH_RATE_LIMIT_PER_MINUTE=10
API_RATE_LIMIT_PER_MINUTE=60
```

### Web Portal

Create `web/.env` from `web/.env.example`.

Important variables:

- `BACKEND_URL`
- `PUBLIC_BASE_URL`
- `APP_SECRET_KEY`
- `SECURE_COOKIES`

Default example:

```env
BACKEND_URL=http://localhost:8000
PUBLIC_BASE_URL=http://127.0.0.1:3000
APP_SECRET_KEY=change-me
SECURE_COOKIES=false
```

### CLI

The CLI does not require a `.env` file for normal local use. It reads:

- `INSIGHTA_API_URL` if you want a default backend URL
- `~/.insighta/credentials.json` for stored login credentials after authentication

## How To Run The Entire System

Run the components in this order:

1. Start the backend
2. Start the web portal
3. Install and use the CLI

### 1. Run the Backend

From the repository root:

```powershell
cd backend
Copy-Item .env.example .env
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The backend will be available at:

- `http://127.0.0.1:8000`

Useful notes:

- `python seed.py` loads seed data for local development
- if you use SQLite instead of PostgreSQL, update `DATABASE_URL` in `backend/.env`
- the backend must be running before the web portal or CLI login flow will work

### 2. Run the Web Portal

Open a new terminal:

```powershell
cd web
Copy-Item .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 3000
```

The web portal will be available at:

- `http://127.0.0.1:3000`

Make sure:

- `BACKEND_URL` points to the backend, usually `http://localhost:8000`
- `PUBLIC_BASE_URL` matches the browser URL you are opening, usually `http://127.0.0.1:3000`

### 3. Install and Run the CLI

Open another terminal:

```powershell
cd cli
pip install .
```

Then authenticate:

```powershell
insighta login --api-url http://127.0.0.1:8000
```

This will:

- start a temporary local callback server on `127.0.0.1`
- open the GitHub OAuth page in your browser
- exchange the callback code with the backend
- store credentials in `~/.insighta/credentials.json`

## Running Each Section Independently

### Backend Only

Use this when you want to test API endpoints directly:

```powershell
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Example health check:

```powershell
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8000/health"
```

### Web Portal Only

Use this when the backend is already running:

```powershell
cd web
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 3000
```

Then open:

```text
http://127.0.0.1:3000
```

### CLI Only

Use this when the backend is already running:

```powershell
cd cli
pip install .
insighta login --api-url http://127.0.0.1:8000
insighta whoami
```

## CLI Usage

Common commands:

```powershell
insighta login --api-url http://127.0.0.1:8000
insighta whoami
insighta profiles list --gender male --country NG
insighta profiles get <profile-id>
insighta profiles search "young males from nigeria"
insighta profiles create --name "Harriet Tubman"
insighta profiles export --format csv
insighta logout
```

Behavior notes:

- `insighta profiles create` requires an `admin` token
- `insighta profiles export` saves `profiles_export.csv` to the current working directory
- the CLI automatically refreshes access tokens when possible

## Web Portal Usage

After starting the web app:

1. Open `http://127.0.0.1:3000`
2. Click the login button
3. Complete the GitHub OAuth flow
4. Use the dashboard, profiles, search, and account pages

The web portal:

- stores access and refresh tokens in HTTP-only cookies
- refreshes server-side when the backend returns `401`
- protects form submissions with a CSRF token

## API Highlights

### Auth Endpoints

- `GET /auth/github`
- `POST /auth/github/callback`
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /auth/me`

### User and Profile Endpoints

- `GET /api/users/me`
- `GET /api/dashboard`
- `GET /api/profiles`
- `GET /api/profiles/search`
- `GET /api/profiles/export?format=csv`
- `GET /api/profiles/{profile_id}`
- `POST /api/profiles`
- `DELETE /api/profiles/{profile_id}`
- `GET /api/admin/users`
- `PATCH /api/admin/users/{user_id}`

### Versioning Requirement

Protected versioned API routes require:

```http
X-API-Version: 1
```

## Local Testing Tips

### Test Auth Tokens

When test auth is enabled on the backend, you can request generated test tokens without completing GitHub OAuth:

Admin token:

```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "http://127.0.0.1:8000/auth/github/callback" `
  -ContentType "application/json" `
  -Body '{"code":"test_code_admin","state":"admin-state"}'
```

Analyst token:

```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "http://127.0.0.1:8000/auth/github/callback" `
  -ContentType "application/json" `
  -Body '{"code":"test_code_analyst","state":"analyst-state"}'
```

Each response includes:

- `access_token`
- `refresh_token`
- `expires_in`
- `refresh_expires_in`
- `user`

### Refresh a Token

```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "http://127.0.0.1:8000/auth/refresh" `
  -ContentType "application/json" `
  -Body '{"refresh_token":"PASTE_REFRESH_TOKEN_HERE"}'
```

### Check the Current User

```powershell
Invoke-RestMethod `
  -Method GET `
  -Uri "http://127.0.0.1:8000/api/users/me" `
  -Headers @{
    Authorization = "Bearer PASTE_ACCESS_TOKEN_HERE"
    "X-API-Version" = "1"
  }
```

## Natural Language Search

The search parser is intentionally rule-based and inherited from Stage 2. It maps words and phrases into structured filters without AI.

Supported query ideas include:

- gender words such as `male`, `female`, `men`, `women`
- age groups such as `child`, `teenager`, `adult`, `senior`
- age ranges such as `above 30`, `under 20`, `between 25 and 40`
- `young` as a shorthand for ages `16-24`
- country names and demonyms mapped to ISO country codes

## Deployment Notes

Each service includes deployment assets:

- `backend/Dockerfile`
- `backend/Procfile`
- `web/Dockerfile`
- `web/Procfile`
- `cli/Dockerfile`

Each app also includes its own CI workflow under its `.github/workflows/` directory.

## Troubleshooting

### PowerShell `curl` Error

If `curl` fails in PowerShell with a headers error, use `Invoke-RestMethod` instead. PowerShell aliases `curl` to `Invoke-WebRequest`, which does not accept `-H` like Unix `curl`.

### OAuth Redirect Problems

Check:

- backend GitHub client IDs and secrets
- web `PUBLIC_BASE_URL`
- backend `CORS_ORIGINS`
- that the backend and web portal are running on the expected ports

### Unauthorized or Version Errors

If an API call fails:

- confirm the access token is present and valid
- confirm `X-API-Version: 1` is included
- confirm the token role has permission for the endpoint

## Additional Component Docs

For component-specific notes, see:

- `backend/READMEE.md`
- `web/README.md`
- `cli/README.md`
