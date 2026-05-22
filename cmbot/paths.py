"""Central path and runtime context for single-user and multi-user modes."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = ROOT / "user_data"

USER_ID = os.environ.get("USER_ID", "")
IS_ADMIN = os.environ.get("IS_ADMIN", "false").lower() == "true"

GLOBAL_TOKENS_FILE = ROOT / "session_tokens.json"
GLOBAL_CONFIG_FILE = ROOT / "config.json"
USERS_FILE = ROOT / "users.json"

API_BASE = "https://api.codementor.io"
REQUESTS_ENDPOINT = f"{API_BASE}/api/v2/requests/search?search_type=all"

if USER_ID:
    REQUESTS_DB = Path(
        os.environ.get("USER_REQUESTS_FILE", USER_DATA_DIR / f"{USER_ID}_requests.json")
    )
    USER_TOKENS_FILE = Path(
        os.environ.get("USER_TOKENS_FILE", USER_DATA_DIR / f"{USER_ID}_tokens.json")
    )
    CONFIG_FILE = Path(
        os.environ.get("USER_CONFIG_FILE", USER_DATA_DIR / f"{USER_ID}_bot_config.json")
    )
else:
    REQUESTS_DB = ROOT / "requests_db.json"
    USER_TOKENS_FILE = GLOBAL_TOKENS_FILE
    CONFIG_FILE = GLOBAL_CONFIG_FILE
