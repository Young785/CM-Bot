"""Resolve per-user Codementor credentials from users.json and user_data."""

from __future__ import annotations

from typing import Dict

from cmbot.paths import USER_DATA_DIR, USERS_FILE
from cmbot.storage.json_store import load_json


def get_user_credentials(user_id: str) -> Dict:
    """Merge A1 credentials: users.json is source of truth, data file overrides."""
    users = load_json(USERS_FILE, {})
    record = users.get(user_id, {}) if isinstance(users, dict) else {}
    data = load_json(USER_DATA_DIR / f"{user_id}_data.json", {})
    if not isinstance(data, dict):
        data = {}

    def pick(key):
        val = data.get(key)
        if val is None or val == "":
            val = record.get(key)
        return val

    return {
        "a1_email": pick("a1_email") or "",
        "a1_password": pick("a1_password") or "",
        "message": pick("message") or record.get("message") or data.get("message") or "",
        "email": record.get("email") or data.get("email") or "",
        "onboarding_complete": record.get("onboarding_complete") or data.get("onboarding_complete"),
        "is_admin": record.get("is_admin", False),
    }
