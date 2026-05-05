# Insighta Web Portal

Server-rendered FastAPI portal that logs users in through GitHub OAuth with PKCE, exchanges the code with the shared backend, stores access and refresh tokens in HTTP-only cookies, and protects all form submissions with CSRF tokens.
The profiles page also supports admin CSV uploads through the shared backend upload API.


## Run

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --port 3000
```
