"""Aggregate bot metrics and render report emails."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from cmbot.auth import load_merged_tokens
from cmbot.storage.json_store import load_json

USERS_FILE = Path("users.json")
USER_DATA_DIR = Path("user_data")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", ""))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _period_window(period: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now = now or datetime.now()
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = start.strftime("%A, %B %d, %Y")
    elif period == "weekly":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        label = f"Week of {start.strftime('%B %d, %Y')}"
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = start.strftime("%B %Y")
    else:
        start = now - timedelta(hours=1)
        label = "Test snapshot"
    return start, now, label


def _request_in_period(processed_at: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse_dt(processed_at)
    if not dt:
        return False
    return start <= dt <= end


def collect_report_data(
    period: str,
    *,
    bot_processes: dict | None = None,
    bot_schedulers: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build structured report payload for all users."""
    now = _naive(now or datetime.now())
    start, end, period_label = _period_window(period, now)
    start, end = _naive(start), _naive(end)
    users_data = load_json(USERS_FILE, {})
    bot_processes = bot_processes or {}
    bot_schedulers = bot_schedulers or {}

    users: list[dict[str, Any]] = []
    total_requests = 0
    period_processed = 0
    onboarded = 0
    schedulers_running = 0
    bots_running = 0
    recent: list[dict[str, Any]] = []

    for user_id, user_data in users_data.items():
        reqs = load_json(USER_DATA_DIR / f"{user_id}_requests.json", {})
        tokens = load_merged_tokens(USER_DATA_DIR / f"{user_id}_tokens.json")
        count = len(reqs)
        total_requests += count

        user_period = 0
        for rid, req in reqs.items():
            if _request_in_period(req.get("processed_at"), start, end):
                user_period += 1
                period_processed += 1
                recent.append(
                    {
                        "user_email": user_data.get("email", user_id),
                        "title": req.get("title", "Untitled"),
                        "author": req.get("author", ""),
                        "budget": req.get("budget", ""),
                        "processed_at": req.get("processed_at", ""),
                        "request_id": rid,
                    }
                )

        is_onboarded = bool(user_data.get("onboarding_complete"))
        if is_onboarded:
            onboarded += 1

        sched = bot_schedulers.get(user_id)
        sched_running = bool(sched and getattr(sched, "running", False))
        if sched_running:
            schedulers_running += 1

        proc = bot_processes.get(user_id, {})
        proc_alive = False
        if proc.get("process") is not None and proc["process"].poll() is None:
            proc_alive = True
            bots_running += 1

        users.append(
            {
                "id": user_id,
                "email": user_data.get("email", "Unknown"),
                "a1_email": user_data.get("a1_email") or "—",
                "onboarding_complete": is_onboarded,
                "scheduler_enabled": bool(user_data.get("scheduler_enabled")),
                "scheduler_running": sched_running,
                "bot_running": proc_alive,
                "has_a1_token": bool(tokens.get("A1", {}).get("access_token")),
                "has_a2_token": bool(tokens.get("A2", {}).get("access_token")),
                "total_requests": count,
                "period_processed": user_period,
            }
        )

    recent.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    recent = recent[:15]

    return {
        "period": period,
        "period_label": period_label,
        "generated_at": now.isoformat(),
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "summary": {
            "total_users": len(users_data),
            "onboarded_users": onboarded,
            "total_requests": total_requests,
            "period_processed": period_processed,
            "active_bots": bots_running,
            "schedulers_running": schedulers_running,
        },
        "users": sorted(users, key=lambda u: u["period_processed"], reverse=True),
        "recent_activity": recent,
    }


def _render_html(data: dict[str, Any]) -> str:
    s = data["summary"]
    period = data["period"].title()
    rows = "".join(
        f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;">{escape(u['email'])}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;text-align:center;">{u['total_requests']}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;text-align:center;">{u['period_processed']}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;text-align:center;">{'Yes' if u['bot_running'] else 'No'}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;text-align:center;">{'Yes' if u['scheduler_running'] else 'No'}</td>
        </tr>"""
        for u in data["users"]
    )
    recent_rows = "".join(
        f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;">{escape(r['user_email'])}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;">{escape(r['title'][:80])}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;">{escape(str(r.get('budget', '')))}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;">{escape((r.get('processed_at') or '')[:19])}</td>
        </tr>"""
        for r in data["recent_activity"]
    ) or '<tr><td colspan="4" style="padding:12px;color:#64748b;">No requests processed in this period.</td></tr>'

    return f"""<!DOCTYPE html>
<html><body style="font-family:Inter,Arial,sans-serif;background:#f4f7f6;margin:0;padding:24px;color:#1e293b;">
  <div style="max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;border:1px solid #e2e8f0;">
    <h1 style="margin:0 0 6px;font-size:22px;color:#7267ef;">CM Bot — {period} Report</h1>
    <p style="margin:0 0 20px;color:#64748b;">{escape(data['period_label'])}</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <tr><td style="padding:10px;background:#f8fafc;border-radius:8px;"><strong>Total users</strong></td><td style="padding:10px;text-align:right;">{s['total_users']}</td></tr>
      <tr><td style="padding:10px;"><strong>Onboarded</strong></td><td style="padding:10px;text-align:right;">{s['onboarded_users']}</td></tr>
      <tr><td style="padding:10px;background:#f8fafc;"><strong>Total requests (all time)</strong></td><td style="padding:10px;text-align:right;">{s['total_requests']}</td></tr>
      <tr><td style="padding:10px;"><strong>Processed this period</strong></td><td style="padding:10px;text-align:right;color:#2ca87f;font-weight:700;">{s['period_processed']}</td></tr>
      <tr><td style="padding:10px;background:#f8fafc;"><strong>Bots running now</strong></td><td style="padding:10px;text-align:right;">{s['active_bots']}</td></tr>
      <tr><td style="padding:10px;"><strong>Schedulers running</strong></td><td style="padding:10px;text-align:right;">{s['schedulers_running']}</td></tr>
    </table>
    <h2 style="font-size:16px;margin:0 0 12px;">Per user</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">
      <thead><tr style="background:#f8fafc;">
        <th style="padding:8px;text-align:left;">User</th>
        <th style="padding:8px;">Total</th>
        <th style="padding:8px;">Period</th>
        <th style="padding:8px;">Bot</th>
        <th style="padding:8px;">Scheduler</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <h2 style="font-size:16px;margin:0 0 12px;">Recent activity</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="background:#f8fafc;">
        <th style="padding:8px;text-align:left;">User</th>
        <th style="padding:8px;text-align:left;">Request</th>
        <th style="padding:8px;">Budget</th>
        <th style="padding:8px;">Processed</th>
      </tr></thead>
      <tbody>{recent_rows}</tbody>
    </table>
    <p style="margin-top:24px;font-size:12px;color:#94a3b8;">Generated {escape(data['generated_at'][:19])} · Codementor Bot</p>
  </div>
</body></html>"""


def _render_text(data: dict[str, Any]) -> str:
    s = data["summary"]
    lines = [
        f"CM Bot — {data['period'].title()} Report",
        data["period_label"],
        "",
        f"Total users: {s['total_users']}",
        f"Onboarded: {s['onboarded_users']}",
        f"Total requests: {s['total_requests']}",
        f"Processed this period: {s['period_processed']}",
        f"Bots running: {s['active_bots']}",
        f"Schedulers running: {s['schedulers_running']}",
        "",
        "Per user:",
    ]
    for u in data["users"]:
        lines.append(
            f"  - {u['email']}: {u['total_requests']} total, "
            f"{u['period_processed']} this period, bot={'on' if u['bot_running'] else 'off'}"
        )
    lines.append("")
    lines.append("Recent activity:")
    if not data["recent_activity"]:
        lines.append("  (none)")
    for r in data["recent_activity"][:10]:
        lines.append(f"  - {r['title'][:60]} ({r['user_email']})")
    return "\n".join(lines)


def build_report(
    period: str,
    *,
    bot_processes: dict | None = None,
    bot_schedulers: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    data = collect_report_data(
        period,
        bot_processes=bot_processes,
        bot_schedulers=bot_schedulers,
        now=now,
    )
    subject_period = "Test" if period == "test" else period.title()
    subject = f"CM Bot {subject_period} Report — {data['period_label']}"
    return {
        "data": data,
        "subject": subject,
        "html": _render_html(data),
        "text": _render_text(data),
    }
