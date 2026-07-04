"""Shared A2 scan cache — one scanner pass serves all user accounts."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cmbot.api.client import CodementorAPI
from cmbot.api.models import Request
from cmbot.auth.service import TokenAuthService
from cmbot.auth.tokens import load_merged_tokens
from cmbot.paths import GLOBAL_CONFIG_FILE, GLOBAL_TOKENS_FILE, USER_DATA_DIR
from cmbot.storage.json_store import load_json, save_json

logger = logging.getLogger(__name__)

GLOBAL_A2_SCAN_FILE = USER_DATA_DIR / "global_a2_scan.json"
DEFAULT_MAX_AGE_MINUTES = 5


def _max_age_minutes() -> int:
    cfg = load_json(GLOBAL_CONFIG_FILE, {})
    try:
        return max(1, min(60, int(cfg.get("check_interval_minutes", DEFAULT_MAX_AGE_MINUTES))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_MINUTES


def load_shared_scan(*, max_age_minutes: int | None = None) -> tuple[list[Request] | None, str | None]:
    """Return cached A2 requests if fresh enough."""
    if not GLOBAL_A2_SCAN_FILE.exists():
        return None, None
    data = load_json(GLOBAL_A2_SCAN_FILE, {})
    scanned_at = data.get("scanned_at")
    if not scanned_at:
        return None, None
    try:
        last = datetime.fromisoformat(str(scanned_at).replace("Z", ""))
        age_limit = max_age_minutes if max_age_minutes is not None else _max_age_minutes()
        if datetime.now() - last > timedelta(minutes=age_limit):
            return None, scanned_at
    except (ValueError, TypeError):
        return None, scanned_at
    items = data.get("requests") or []
    return [Request.from_dict(r) for r in items if isinstance(r, dict)], scanned_at


def load_stale_shared_scan() -> tuple[list[Request] | None, str | None]:
    """Return cache regardless of age (fallback when live scan fails)."""
    if not GLOBAL_A2_SCAN_FILE.exists():
        return None, None
    data = load_json(GLOBAL_A2_SCAN_FILE, {})
    scanned_at = data.get("scanned_at")
    items = data.get("requests") or []
    if not items:
        return None, scanned_at
    return [Request.from_dict(r) for r in items if isinstance(r, dict)], scanned_at


def save_shared_scan(requests: list[Request]) -> None:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(
        GLOBAL_A2_SCAN_FILE,
        {
            "scanned_at": datetime.now().isoformat(),
            "count": len(requests),
            "requests": [r.to_dict() for r in requests],
        },
    )


async def _scan_a2_once() -> list[Request] | None:
    """Refresh A2 if needed and scan once using global credentials."""
    old_env = {
        k: os.environ.get(k)
        for k in ("USER_ID", "IS_ADMIN", "USER_TOKENS_FILE", "USER_REQUESTS_FILE", "USER_CONFIG_FILE")
    }
    try:
        os.environ.pop("USER_ID", None)
        os.environ["IS_ADMIN"] = "true"
        os.environ.pop("USER_TOKENS_FILE", None)
        os.environ.pop("USER_REQUESTS_FILE", None)
        os.environ.pop("USER_CONFIG_FILE", None)

        tokens = load_merged_tokens(GLOBAL_TOKENS_FILE)
        auth = TokenAuthService(tokens, TokenAuthService().config)
        a2_email = auth.config.get("a2_email", "")

        async with CodementorAPI(tokens) as api:
            result, status = await api.try_scan_raw("A2")
            if result is not None:
                return result
            if status not in (0, 401):
                logger.error("A2 scan failed (HTTP %s)", status)
                return None

        if await auth.ensure_valid_token("A2", a2_email):
            tokens = load_merged_tokens(GLOBAL_TOKENS_FILE)
            async with CodementorAPI(tokens) as api:
                result, status = await api.try_scan_raw("A2")
                if result is not None:
                    return result
                logger.error("A2 scan failed after refresh (HTTP %s)", status)
        return None
    finally:
        for key, val in old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def ensure_shared_a2_scan(*, force: bool = False) -> tuple[bool, str]:
    """
    Ensure global A2 scan cache is fresh. Returns (success, message).
    Serialized by caller (app holds the lock).
    """
    if not force:
        cached, scanned_at = load_shared_scan()
        if cached is not None:
            return True, f"Using cached A2 scan ({len(cached)} requests, {scanned_at})"

    try:
        requests = asyncio.run(asyncio.wait_for(_scan_a2_once(), timeout=120))
    except asyncio.TimeoutError:
        return False, "A2 scan timed out"
    except Exception as e:
        logger.exception("Shared A2 scan error")
        stale, scanned_at = load_stale_shared_scan()
        if stale:
            return True, f"A2 scan failed ({e}); using stale cache ({len(stale)} requests from {scanned_at})"
        return False, str(e)

    if not requests:
        stale, scanned_at = load_stale_shared_scan()
        if stale:
            return True, f"A2 scan returned nothing; using stale cache ({len(stale)} requests from {scanned_at})"
        return False, "A2 scan failed — check admin A2 token in Tokens page"

    save_shared_scan(requests)
    return True, f"A2 scan complete ({len(requests)} requests cached for all users)"
