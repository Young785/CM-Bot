"""JSON file persistence helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(filepath: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not filepath.exists():
        return default
    try:
        content = filepath.read_text(encoding="utf-8").strip()
        if not content:
            return default
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Could not load {filepath}: {e}")
        return default


def save_json(filepath: Path, data: Any) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_json_dict(path: Path) -> dict:
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def extract_refresh_token(cookie_string: str) -> str:
    if not cookie_string or not isinstance(cookie_string, str):
        return ""
    if "REFRESH_TOKEN=" in cookie_string:
        return cookie_string.split("REFRESH_TOKEN=", 1)[1].split(";")[0].strip()
    return ""


def extract_access_token(cookie_string: str) -> str:
    """Extract ACCESS_TOKEN from a cookie string or raw token value."""
    if not cookie_string:
        return ""
    if isinstance(cookie_string, dict):
        return cookie_string.get("access_token", "")
    if not isinstance(cookie_string, str):
        return str(cookie_string)
    if "ACCESS_TOKEN=" in cookie_string:
        part = cookie_string.split("ACCESS_TOKEN=", 1)[1]
        return part.split(";")[0].strip()
    if cookie_string.startswith("eyJ"):
        return cookie_string
    return cookie_string.strip()
