"""Background scheduler for daily, weekly, and monthly report emails."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from cmbot.reports.builder import build_report
from cmbot.reports.emailer import get_report_settings, send_report_email
from cmbot.storage.json_store import load_json, save_json

logger = logging.getLogger(__name__)

STATE_FILE = Path("user_data/report_scheduler_state.json")

_report_scheduler: "ReportScheduler | None" = None


class ReportScheduler:
    def __init__(
        self,
        *,
        get_bot_processes: Callable[[], dict],
        get_bot_schedulers: Callable[[], dict],
    ):
        self._get_bot_processes = get_bot_processes
        self._get_bot_schedulers = get_bot_schedulers
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        settings = get_report_settings()
        if not settings["enabled"]:
            logger.info("Report scheduler disabled (REPORTS_ENABLED=0)")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="report-scheduler")
        self._thread.start()
        logger.info("Report scheduler started (to=%s)", settings["to"])

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def send_now(self, period: str) -> dict:
        with self._lock:
            return self._deliver(period, mark_state=period != "test")

    def _load_state(self) -> dict:
        return load_json(STATE_FILE, {})

    def _save_state(self, state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_json(STATE_FILE, state)

    def _now(self) -> datetime:
        settings = get_report_settings()
        try:
            return datetime.now(ZoneInfo(settings["timezone"]))
        except Exception:
            return datetime.now()

    def _deliver(self, period: str, *, mark_state: bool) -> dict:
        report_period = "daily" if period == "test" else period
        built = build_report(
            report_period,
            bot_processes=self._get_bot_processes(),
            bot_schedulers=self._get_bot_schedulers(),
            now=self._now(),
        )
        if period == "test":
            built["subject"] = "[TEST] " + built["subject"]

        result = send_report_email(
            subject=built["subject"],
            html_body=built["html"],
            text_body=built["text"],
        )
        if result.get("success") and mark_state:
            state = self._load_state()
            key = period if period in ("daily", "weekly", "monthly") else "daily"
            now = self._now()
            if key == "daily":
                state["last_daily"] = now.date().isoformat()
            elif key == "weekly":
                state["last_weekly"] = now.date().isoformat()
            elif key == "monthly":
                state["last_monthly"] = now.strftime("%Y-%m")
            state["last_sent_at"] = now.isoformat()
            self._save_state(state)
        result["period"] = period
        result["subject"] = built["subject"]
        return result

    def _should_send(self, period: str, now: datetime, state: dict) -> bool:
        settings = get_report_settings()
        if now.hour != settings["daily_hour"] or now.minute != 0:
            return False

        today = now.date().isoformat()
        if period == "daily":
            return state.get("last_daily") != today
        if period == "weekly":
            if now.weekday() != settings["weekly_weekday"]:
                return False
            return state.get("last_weekly") != today
        if period == "monthly":
            if now.day != settings["monthly_day"]:
                return False
            month_key = now.strftime("%Y-%m")
            return state.get("last_monthly") != month_key
        return False

    def _loop(self) -> None:
        while self._running:
            try:
                now = self._now()
                state = self._load_state()
                for period in ("daily", "weekly", "monthly"):
                    if self._should_send(period, now, state):
                        with self._lock:
                            result = self._deliver(period, mark_state=True)
                        if result.get("success"):
                            logger.info("Sent %s report to %s", period, result.get("to"))
                        else:
                            logger.warning("Failed %s report: %s", period, result.get("error"))
            except Exception as exc:
                logger.exception("Report scheduler error: %s", exc)
            time.sleep(60)


def get_report_scheduler(
    *,
    get_bot_processes: Callable[[], dict],
    get_bot_schedulers: Callable[[], dict],
) -> ReportScheduler:
    global _report_scheduler
    if _report_scheduler is None:
        _report_scheduler = ReportScheduler(
            get_bot_processes=get_bot_processes,
            get_bot_schedulers=get_bot_schedulers,
        )
    return _report_scheduler
