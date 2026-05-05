import os
import sys
import unittest
from pathlib import Path
import asyncio


ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TEST_DB_PATH = ROOT / "backend" / "contract_test.sqlite3"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["APP_SECRET_KEY"] = "test-secret-key-that-is-long-enough"
os.environ["GITHUB_CLIENT_ID"] = "test-client-id"
os.environ["GITHUB_CLIENT_SECRET"] = "test-client-secret"
os.environ["ENABLE_TEST_AUTH"] = "true"

from fastapi.testclient import TestClient

from app.cache import query_cache
from app.dependencies import _rate_windows
from app.main import app
from app.models import oauth_states, profiles, refresh_tokens, users


class AuthApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        _rate_windows.clear()
        asyncio.run(query_cache.clear())
        self._clear_tables()

    def _clear_tables(self):
        from app.db import engine

        with engine.begin() as connection:
            connection.execute(oauth_states.delete())
            connection.execute(profiles.delete())
            connection.execute(refresh_tokens.delete())
            connection.execute(users.delete())

    def _issue_test_tokens(self, role: str) -> dict:
        response = self.client.post(
            "/auth/github/callback",
            json={"code": f"test_code_{role}", "state": f"state-{role}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("access_token", payload)
        self.assertIn("refresh_token", payload)
        self.assertEqual(payload["user"]["role"], role)
        return payload

    def test_auth_refresh_and_logout_only_allow_post_with_json_errors(self):
        refresh_response = self.client.get("/auth/refresh")
        logout_response = self.client.get("/auth/logout")

        self.assertEqual(refresh_response.status_code, 405)
        self.assertEqual(logout_response.status_code, 405)
        self.assertEqual(
            refresh_response.json()["message"],
            "Method Not Allowed",
        )
        self.assertEqual(
            logout_response.json()["message"],
            "Method Not Allowed",
        )

    def test_auth_flow_returns_access_and_refresh_tokens_for_admin_and_analyst(self):
        admin_payload = self._issue_test_tokens("admin")
        analyst_payload = self._issue_test_tokens("analyst")

        self.assertEqual(admin_payload["token_type"], "bearer")
        self.assertGreater(admin_payload["expires_in"], 0)
        self.assertGreater(admin_payload["refresh_expires_in"], 0)
        self.assertEqual(analyst_payload["token_type"], "bearer")
        self.assertGreater(analyst_payload["expires_in"], 0)
        self.assertGreater(analyst_payload["refresh_expires_in"], 0)

    def test_auth_github_returns_cors_headers_for_browser_requests(self):
        response = self.client.get(
            "/auth/github",
            params={
                "client": "web",
                "redirect_uri": "http://localhost:8000/auth/callback",
                "state": "browser-state",
                "code_challenge": "browser-challenge",
            },
            headers={"Origin": "http://localhost:3000"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://localhost:3000",
        )
        self.assertEqual(
            response.headers.get("access-control-allow-credentials"),
            "true",
        )

    def test_users_me_requires_valid_token_and_api_version(self):
        analyst_payload = self._issue_test_tokens("analyst")
        headers = {"Authorization": f"Bearer {analyst_payload['access_token']}"}

        unauthenticated = self.client.get("/api/users/me", headers={"X-API-Version": "1"})
        missing_version = self.client.get("/api/users/me", headers=headers)
        success = self.client.get(
            "/api/users/me",
            headers={**headers, "X-API-Version": "1"},
        )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.json()["message"], "Authentication required")
        self.assertEqual(missing_version.status_code, 400)
        self.assertEqual(missing_version.json()["message"], "API version header required")
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(success.json()["data"]["role"], "analyst")

    def test_role_enforcement_allows_admin_and_blocks_analyst_from_admin_route(self):
        admin_payload = self._issue_test_tokens("admin")
        analyst_payload = self._issue_test_tokens("analyst")

        analyst_response = self.client.get(
            "/api/admin/users",
            headers={
                "Authorization": f"Bearer {analyst_payload['access_token']}",
                "X-API-Version": "1",
            },
        )
        admin_response = self.client.get(
            "/api/admin/users",
            headers={
                "Authorization": f"Bearer {admin_payload['access_token']}",
                "X-API-Version": "1",
            },
        )

        self.assertEqual(analyst_response.status_code, 403)
        self.assertEqual(analyst_response.json()["message"], "Forbidden")
        self.assertEqual(admin_response.status_code, 200, admin_response.text)

    def test_auth_github_rate_limit_hits_429_after_ten_requests(self):
        for attempt in range(10):
            response = self.client.get(
                "/auth/github",
                params={
                    "client": "web",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "state": f"rate-limit-state-{attempt}",
                    "code_challenge": "rate-limit-challenge",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302, response.text)

        blocked = self.client.get(
            "/auth/github",
            params={
                "client": "web",
                "redirect_uri": "http://localhost:8000/auth/callback",
                "state": "rate-limit-state-11",
                "code_challenge": "rate-limit-challenge",
            },
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["message"], "Rate limit exceeded")


if __name__ == "__main__":
    unittest.main()
