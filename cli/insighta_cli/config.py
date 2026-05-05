import json
import os
from pathlib import Path


APP_DIR = Path.home() / ".insighta"
CREDENTIALS_PATH = APP_DIR / "credentials.json"
DEFAULT_API_URL = os.getenv("INSIGHTA_API_URL", "http://localhost:8000")


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_credentials() -> dict | None:
    if not CREDENTIALS_PATH.exists():
        return None
    return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))


def save_credentials(data: dict) -> None:
    ensure_app_dir()
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_credentials() -> None:
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
