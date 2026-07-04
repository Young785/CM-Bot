"""Hybrid bot: A2 API scan + A1 API processing with automated auth."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp

from cmbot.api.client import CM_HEADERS, CodementorAPI
from cmbot.api.models import Request
from cmbot.auth.service import TokenAuthService
from cmbot.auth.tokens import load_tokens
from cmbot.paths import CONFIG_FILE, REQUESTS_DB, USER_ID, IS_ADMIN
from cmbot.storage.json_store import load_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_MESSAGE = (
    "I am an experienced Software Engineer with many years experience in different "
    "stacks and i will like to show interest in your request"
)


class CodementorBot:
    def __init__(self):
        self.tokens = load_tokens()
        self.auth = TokenAuthService(self.tokens, self._load_config())
        self.storage = self._load_storage()
        self.config = self.auth.config

    def _load_config(self) -> Dict:
        return TokenAuthService().config

    def _load_storage(self) -> Dict[str, Request]:
        if not REQUESTS_DB.exists():
            return {}
        data = load_json(REQUESTS_DB, {})
        if not isinstance(data, dict):
            return {}
        return {k: Request.from_dict(v) for k, v in data.items()}

    def save_storage(self):
        with open(REQUESTS_DB, "w", encoding="utf-8") as f:
            import json

            json.dump({k: v.to_dict() for k, v in self.storage.items()}, f, indent=2)

    async def scan_with_retry(self, account: str, email: str) -> Optional[List[Request]]:
        async with CodementorAPI(self.tokens) as api:
            result, status = await api.try_scan_raw(account)
            if result is not None:
                return result
            # status 0 = missing token, 401 = expired
            if status not in (0, 401):
                logger.error("%s scan failed (HTTP %s)", account, status)
                return None

        if status == 0:
            logger.warning("%s has no token — attempting automated login...", account)
        else:
            logger.info("%s token expired — refreshing...", account)
        if await self.auth.ensure_valid_token(account, email):
            self.tokens = load_tokens()
            self.auth.tokens = self.tokens
            async with CodementorAPI(self.tokens) as api:
                result, _ = await api.try_scan_raw(account)
                return result
        return None

    async def process_request_a1_api(self, request: Request) -> bool:
        logger.info("Processing: %s...", request.title[:50])
        a1_token = self.tokens.get("A1", {}).get("access_token", "")
        if not a1_token:
            logger.error("No A1 token")
            return False

        message = self.config.get("message", DEFAULT_MESSAGE)
        cookies = {"ACCESS_TOKEN": a1_token}
        success = False

        async with aiohttp.ClientSession() as session:
            interest_url = f"https://api.codementor.io/api/v2/requests/{request.random_key}/interests"
            try:
                async with session.post(
                    interest_url,
                    headers={**CM_HEADERS, "Content-Type": "application/json;charset=utf-8"},
                    cookies=cookies,
                    json={"message": message, "open_to_special_rate": False},
                ) as resp:
                    if resp.status in (200, 201, 409):
                        success = True
                    else:
                        logger.warning("Interest failed: %s", resp.status)
            except Exception as e:
                logger.error("Interest error: %s", e)

            username = request.author
            if username and success:
                msg_url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
                try:
                    await session.post(
                        msg_url,
                        headers={**CM_HEADERS, "Content-Type": "application/json;charset=utf-8"},
                        cookies=cookies,
                        json={
                            "message": {
                                "content": message,
                                "type": "message",
                                "request": {"temp_message_id": str(uuid.uuid4())},
                            }
                        },
                    )
                except Exception as e:
                    logger.warning("Message error: %s", e)
        return success

    async def run(self):
        logger.info("\n%s\nCODEMENTOR BOT - HYBRID MODE\n%s", "=" * 60, "=" * 60)
        if USER_ID:
            logger.info("User mode: %s", USER_ID)

        a2_email = self.config.get("a2_email", "")
        a1_email = self.config.get("a1_email", "")

        use_shared = os.environ.get("USE_SHARED_A2_SCAN", "").lower() in ("1", "true", "yes")
        if USER_ID and (use_shared or not IS_ADMIN):
            from cmbot.bot.shared_scan import load_shared_scan, load_stale_shared_scan

            logger.info("\n[1/3] Loading shared A2 scan (admin scanner)...")
            a2_requests, scanned_at = load_shared_scan(max_age_minutes=9999)
            if not a2_requests:
                a2_requests, scanned_at = load_stale_shared_scan()
            if not a2_requests:
                logger.error("Shared A2 scan unavailable — admin must refresh scanner cache first")
                return
            logger.info("Using %s requests from shared A2 cache (scanned %s)", len(a2_requests), scanned_at or "?")
        else:
            logger.info("\n[1/3] Scanning with A2...")
            a2_requests = await self.scan_with_retry("A2", a2_email)
            if a2_requests is None:
                logger.error("A2 scan failed")
                return
            logger.info("Found %s requests from A2", len(a2_requests))
            if USER_ID and IS_ADMIN:
                from cmbot.bot.shared_scan import save_shared_scan

                save_shared_scan(a2_requests)
                logger.info("Updated shared A2 cache for all users")

        logger.info("\n[2/3] Scanning A1 interests...")
        a1_requests = await self.scan_with_retry("A1", a1_email)
        if a1_requests is None:
            logger.error("A1 scan failed")
            return
        a1_ids = {r.request_id for r in a1_requests if r.already_interested}
        logger.info("A1 already interested in %s requests", len(a1_ids))

        to_process = [
            r
            for r in a2_requests
            if r.request_id not in self.storage and r.request_id not in a1_ids
        ]
        logger.info("\nNew requests to process: %s", len(to_process))
        if not to_process:
            logger.info("Nothing to do.")
            return

        logger.info("\n[3/3] Processing with A1 API...")
        for req in to_process:
            if await self.process_request_a1_api(req):
                req.processed_at = datetime.now().isoformat()
                self.storage[req.request_id] = req
                self.save_storage()
            await asyncio.sleep(2)

        logger.info("\nDone. Total stored: %s\n", len(self.storage))


async def main():
    bot = CodementorBot()
    await bot.run()


if __name__ == "__main__":
    try:
        import aiohttp  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        print("Installing dependencies...")
        os.system(".venv/bin/pip install aiohttp playwright")
        os.system(".venv/bin/playwright install chromium")
        raise SystemExit(1)
    asyncio.run(main())
