"""SMTP delivery for report emails."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from cmbot.storage.json_store import load_json

CONFIG_FILE = Path("config.json")
DEFAULT_TO = "ayomikunariyo@gmail.com"


def _truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def get_report_settings() -> dict[str, Any]:
    """Merge report/SMTP settings from env and config.json."""
    cfg = load_json(CONFIG_FILE, {})
    reports = cfg.get("reports") or {}
    a1 = cfg.get("account_a1") or {}

    to_addr = (
        os.environ.get("REPORT_EMAIL_TO")
        or reports.get("to")
        or DEFAULT_TO
    ).strip()

    smtp_user = (
        os.environ.get("SMTP_USER")
        or reports.get("smtp_user")
        or a1.get("email")
        or ""
    ).strip()

    smtp_password = (
        os.environ.get("SMTP_PASSWORD")
        or reports.get("smtp_password")
        or ""
    ).strip()

    if not smtp_password and _truthy(os.environ.get("SMTP_USE_A1_PASSWORD", reports.get("use_a1_password", "1"))):
        smtp_password = (a1.get("password") or "").strip()

    return {
        "enabled": _truthy(os.environ.get("REPORTS_ENABLED", str(reports.get("enabled", True)))),
        "to": to_addr,
        "from": (os.environ.get("SMTP_FROM") or reports.get("smtp_from") or smtp_user).strip(),
        "smtp_host": (os.environ.get("SMTP_HOST") or reports.get("smtp_host") or "smtp.gmail.com").strip(),
        "smtp_port": int(os.environ.get("SMTP_PORT") or reports.get("smtp_port") or 587),
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "timezone": (os.environ.get("REPORT_TIMEZONE") or reports.get("timezone") or "UTC").strip(),
        "daily_hour": int(os.environ.get("REPORT_HOUR") or reports.get("daily_hour") or 8),
        "weekly_weekday": int(os.environ.get("REPORT_WEEKLY_WEEKDAY") or reports.get("weekly_weekday") or 0),
        "monthly_day": int(os.environ.get("REPORT_MONTHLY_DAY") or reports.get("monthly_day") or 1),
    }


def send_report_email(
    *,
    subject: str,
    html_body: str,
    text_body: str,
    to_addr: str | None = None,
) -> dict[str, Any]:
    settings = get_report_settings()
    recipient = (to_addr or settings["to"]).strip()
    sender = settings["from"] or settings["smtp_user"]

    if not settings["smtp_user"] or not settings["smtp_password"]:
        return {
            "success": False,
            "error": "SMTP not configured. Set SMTP_USER/SMTP_PASSWORD or config.json reports.smtp_*",
        }
    if not recipient:
        return {"success": False, "error": "No recipient email configured."}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings["smtp_user"], settings["smtp_password"])
            server.sendmail(sender, [recipient], msg.as_string())
        return {"success": True, "to": recipient, "from": sender}
    except smtplib.SMTPAuthenticationError:
        return {
            "success": False,
            "error": "SMTP authentication failed. Use a Gmail App Password if 2FA is enabled.",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
