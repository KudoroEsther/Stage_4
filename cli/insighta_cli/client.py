import time
from pathlib import Path

import httpx
from rich.console import Console

from insighta_cli.config import (
    DEFAULT_API_URL,
    clear_credentials,
    load_credentials,
    resolve_api_url,
    save_credentials,
)


console = Console()


class InsightaClient:
    def __init__(self, api_url: str | None = None):
        self.credentials = load_credentials() or {}
        configured_url = api_url or self.credentials.get("api_url") or DEFAULT_API_URL
        self.api_url = resolve_api_url(configured_url)

    def is_logged_in(self) -> bool:
        return bool(self.credentials.get("access_token") and self.credentials.get("refresh_token"))

    def save_session(self, payload: dict) -> None:
        now = int(time.time())
        self.credentials = {
            "api_url": self.api_url,
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "access_token_expires_at": now + payload.get("expires_in", 180),
            "refresh_token_expires_at": now + payload.get("refresh_expires_in", 300),
            "user": payload.get("user"),
        }
        save_credentials(self.credentials)

    def clear_session(self) -> None:
        self.credentials = {}
        clear_credentials()

    def _refresh_if_needed(self) -> None:
        if not self.is_logged_in():
            raise RuntimeError("You are not logged in. Run `insighta login`.")

        now = int(time.time())
        if now < self.credentials.get("access_token_expires_at", 0) - 10:
            return
        if now >= self.credentials.get("refresh_token_expires_at", 0):
            self.clear_session()
            raise RuntimeError("Your session expired. Run `insighta login` again.")

        response = httpx.post(
            f"{self.api_url}/auth/refresh",
            json={"refresh_token": self.credentials["refresh_token"]},
            timeout=20,
        )
        if response.status_code != 200:
            self.clear_session()
            raise RuntimeError("Unable to refresh session. Run `insighta login` again.")
        payload = response.json()
        payload["user"] = self.credentials.get("user")
        self.save_session(payload)

    def request(self, method: str, path: str, *, params=None, json=None, files=None) -> httpx.Response:
        self._refresh_if_needed()
        headers = {
            "Authorization": f"Bearer {self.credentials['access_token']}",
            "X-API-Version": "1",
        }
        response = httpx.request(
            method,
            f"{self.api_url}{path}",
            headers=headers,
            params=params,
            json=json,
            files=files,
            timeout=30,
        )
        if response.status_code == 401:
            self._refresh_if_needed()
            headers["Authorization"] = f"Bearer {self.credentials['access_token']}"
            response = httpx.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                params=params,
                json=json,
                files=files,
                timeout=30,
            )
        return response

    def logout(self) -> None:
        if self.credentials.get("refresh_token"):
            try:
                httpx.post(
                    f"{self.api_url}/auth/logout",
                    json={"refresh_token": self.credentials["refresh_token"]},
                    timeout=20,
                )
            finally:
                self.clear_session()

    def download_file(self, path: str, output_path: Path, *, params=None) -> Path:
        response = self.request("GET", path, params=params)
        if response.status_code != 200:
            raise RuntimeError(response.json().get("message", "Download failed"))
        output_path.write_bytes(response.content)
        return output_path

    def upload_file(self, path: str, file_path: Path) -> httpx.Response:
        # We open the file only for the duration of the request so large uploads
        # stream from disk instead of being copied into CLI memory first.
        with file_path.open("rb") as file_obj:
            files = {"file": (file_path.name, file_obj, "text/csv")}
            return self.request("POST", path, files=files)
