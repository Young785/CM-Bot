"""Password reset tokens and email delivery."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional

from cmbot.paths import USER_DATA_DIR
from cmbot.reports.emailer import send_report_email
from cmbot.storage.json_store import load_json, save_json

RESET_TOKENS_FILE = USER_DATA_DIR / "password_reset_tokens.json"
TOKEN_TTL_HOURS = 1


def _load_tokens() -> dict:
    data = load_json(RESET_TOKENS_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_tokens(data: dict) -> None:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(RESET_TOKENS_FILE, data)


def _purge_expired(tokens: dict) -> dict:
    now = datetime.now()
    kept = {}
    for key, entry in tokens.items():
        if not isinstance(entry, dict):
            continue
        try:
            exp = datetime.fromisoformat(str(entry.get("expires_at", "")).replace("Z", ""))
            if exp > now:
                kept[key] = entry
        except (ValueError, TypeError):
            continue
    return kept


def create_reset_token(user_id: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    tokens = _purge_expired(_load_tokens())
    tokens[token] = {
        "user_id": user_id,
        "email": email.lower(),
        "expires_at": (datetime.now() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat(),
        "created_at": datetime.now().isoformat(),
    }
    _save_tokens(tokens)
    return token


def peek_reset_token(token: str) -> Optional[dict]:
    """Return token entry if valid, without consuming it."""
    if not token:
        return None
    tokens = _purge_expired(_load_tokens())
    entry = tokens.get(token)
    if not entry or not isinstance(entry, dict):
        return None
    try:
        exp = datetime.fromisoformat(str(entry.get("expires_at", "")).replace("Z", ""))
        if exp <= datetime.now():
            return None
    except (ValueError, TypeError):
        return None
    return entry


def consume_reset_token(token: str) -> Optional[dict]:
    if not token:
        return None
    tokens = _purge_expired(_load_tokens())
    entry = tokens.pop(token, None)
    _save_tokens(tokens)
    if not entry or not isinstance(entry, dict):
        return None
    try:
        exp = datetime.fromisoformat(str(entry.get("expires_at", "")).replace("Z", ""))
        if exp <= datetime.now():
            return None
    except (ValueError, TypeError):
        return None
    return entry


def send_reset_email(*, to_addr: str, reset_url: str) -> dict:
    subject = "Reset your Codementor Bot password"
    text_body = (
        "You requested a password reset for your Codementor Bot account.\n\n"
        f"Open this link to choose a new password (expires in {TOKEN_TTL_HOURS} hour):\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = f"""
    <div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:24px;">
      <h2 style="color:#1e293b;">Reset your password</h2>
      <p style="color:#64748b;line-height:1.6;">
        You requested a password reset for your Codementor Bot account.
        Click the button below to choose a new password. This link expires in {TOKEN_TTL_HOURS} hour.
      </p>
      <p style="margin:28px 0;">
        <a href="{reset_url}" style="background:#7267ef;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">
          Reset password
        </a>
      </p>
      <p style="color:#94a3b8;font-size:13px;">If you did not request this, ignore this email.</p>
    </div>
    """
    return send_report_email(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        to_addr=to_addr,
    )
