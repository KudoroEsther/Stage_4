import asyncio
import io
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TEST_DB_PATH = ROOT / "backend" / "optimization_test.sqlite3"
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
from app.nlp import parse_query
from app.query_utils import build_profiles_cache_key, normalize_profile_filters
from app.security import new_uuid, utcnow


class QueryOptimizationAndUploadTests(unittest.TestCase):
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
        return response.json()

    def _insert_profile(self, *, name: str, age: int = 30, gender: str = "female", country_id: str = "NG"):
        from app.db import engine

        with engine.begin() as connection:
            connection.execute(
                profiles.insert().values(
                    id=new_uuid(),
                    name=name.strip().lower(),
                    gender=gender,
                    gender_probability=0.9,
                    sample_size=10,
                    age=age,
                    age_group="adult",
                    country_id=country_id,
                    country_name="Nigeria",
                    country_probability=0.8,
                    created_at=utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )

    def test_equivalent_search_phrasings_share_the_same_canonical_cache_key(self):
        first_filters = normalize_profile_filters(
            parse_query("Nigerian females between ages 20 and 45")
        )
        second_filters = normalize_profile_filters(
            parse_query("Women aged 20-45 living in Nigeria")
        )

        self.assertEqual(first_filters, second_filters)
        self.assertEqual(
            build_profiles_cache_key(
                namespace="profiles:search",
                filters=first_filters,
                page=1,
                limit=10,
            ),
            build_profiles_cache_key(
                namespace="profiles:search",
                filters=second_filters,
                page=1,
                limit=10,
            ),
        )

    def test_csv_upload_batches_valid_rows_and_skips_invalid_ones(self):
        admin_payload = self._issue_test_tokens("admin")
        self._insert_profile(name="existing person")

        csv_body = "\n".join(
            [
                "name,gender,age,gender_probability,country_id,country_probability,country_name,sample_size",
                "Existing Person,female,28,0.9,NG,0.8,Nigeria,4",
                "Valid Person,female,28,0.9,NG,0.8,Nigeria,4",
                "Negative Age,male,-4,0.8,NG,0.7,Nigeria,3",
                "Missing Country,male,22,0.8,,0.7,,3",
                "Bad Gender,robot,22,0.8,NG,0.7,Nigeria,3",
                "Malformed Only,one,two",
                "Valid Person,female,28,0.9,NG,0.8,Nigeria,4",
            ]
        )

        response = self.client.post(
            "/api/profiles/upload",
            headers={
                "Authorization": f"Bearer {admin_payload['access_token']}",
                "X-API-Version": "1",
            },
            files={"file": ("profiles.csv", io.BytesIO(csv_body.encode("utf-8")), "text/csv")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["total_rows"], 7)
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["skipped"], 6)
        self.assertEqual(payload["reasons"]["duplicate_name"], 2)
        self.assertEqual(payload["reasons"]["invalid_age"], 1)
        self.assertEqual(payload["reasons"]["missing_fields"], 1)
        self.assertEqual(payload["reasons"]["invalid_gender"], 1)
        self.assertEqual(payload["reasons"]["malformed_row"], 1)

        list_response = self.client.get(
            "/api/profiles",
            headers={
                "Authorization": f"Bearer {admin_payload['access_token']}",
                "X-API-Version": "1",
            },
        )
        self.assertEqual(list_response.status_code, 200, list_response.text)
        returned_names = {row["name"] for row in list_response.json()["data"]}
        self.assertIn("valid person", returned_names)
        self.assertIn("existing person", returned_names)


if __name__ == "__main__":
    unittest.main()
