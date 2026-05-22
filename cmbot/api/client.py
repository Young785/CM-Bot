"""Codementor REST API client for request scanning."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from cmbot.api.models import Request
from cmbot.paths import REQUESTS_ENDPOINT

logger = logging.getLogger(__name__)

CM_HEADERS = {
    "X-Requested-From": "cm-web",
    "Origin": "https://www.codementor.io",
    "Referer": "https://www.codementor.io/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
}


class CodementorAPI:
    def __init__(self, tokens: Dict[str, Dict]):
        self.tokens = tokens
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    def _headers_cookies(self, account: str):
        acc = self.tokens.get(account, {})
        token = acc.get("access_token")
        if not token:
            return None, None
        return CM_HEADERS, {"ACCESS_TOKEN": token}

    async def get_requests(self, account: str) -> Optional[List[Request]]:
        headers, cookies = self._headers_cookies(account)
        if not headers:
            return None
        try:
            async with self.session.get(REQUESTS_ENDPOINT, headers=headers, cookies=cookies) as resp:
                if resp.status == 200:
                    return self._parse_requests(await resp.json())
                if resp.status == 401:
                    logger.warning("%s token expired (401)", account)
                    return None
                logger.error("API error for %s: %s", account, resp.status)
                return None
        except Exception as e:
            logger.error("API request failed for %s: %s", account, e)
            return None

    def _parse_requests(self, data: Any) -> List[Request]:
        requests = []
        items = data if isinstance(data, list) else data.get("requests", data.get("data", []))
        for item in items:
            try:
                req_id = str(item.get("id", item.get("slug", "")))
                random_key = item.get("random_key", req_id)
                if not req_id:
                    continue
                requests.append(
                    Request(
                        request_id=req_id,
                        random_key=str(random_key),
                        title=item.get("title", "Untitled"),
                        author=item.get("user", {}).get("username", "Unknown"),
                        budget=str(item.get("estimated_budget", "")),
                        request_type=item.get("request_type", "Unknown"),
                        tags=item.get("tag_list", []) or [],
                        interested_count=item.get("interest_count", 0),
                        posted_time=str(item.get("created_at", "")),
                        url=f"https://www.codementor.io/m/dashboard/open-requests/{req_id}",
                        description=item.get("body", ""),
                        already_interested=item.get("already_interested", False),
                    )
                )
            except Exception:
                continue
        return requests

    async def try_scan_raw(self, account: str) -> tuple[Optional[List[Request]], int]:
        """Returns (requests, http_status). status 401 means token refresh needed."""
        headers, cookies = self._headers_cookies(account)
        if not headers:
            return None, 0
        try:
            async with self.session.get(REQUESTS_ENDPOINT, headers=headers, cookies=cookies) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = data.get("data") or data.get("requests") or data.get("results") or []
                    else:
                        return None, resp.status
                    return self._parse_from_items(items), 200
                return None, resp.status
        except Exception as e:
            logger.error("Scan error: %s", e)
            return None, 0

    def _parse_from_items(self, items) -> List[Request]:
        requests = []
        for item in items:
            if not isinstance(item, dict):
                continue
            req_id = item.get("id", item.get("slug", "")) or item.get("random_key", "")
            random_key = item.get("random_key", req_id)
            if not req_id:
                continue
            requests.append(
                Request(
                    request_id=str(req_id),
                    random_key=str(random_key),
                    title=item.get("title", ""),
                    author=item.get("user", {}).get("username", item.get("author", "Unknown")),
                    budget=str(item.get("budget", item.get("estimated_budget", 0))),
                    request_type=item.get("type", item.get("request_type", "")),
                    tags=item.get("tag_list", item.get("tags", [])) or [],
                    interested_count=item.get("interest_count", 0),
                    posted_time=str(item.get("created_at", "")),
                    url=f"https://www.codementor.io/m/dashboard/open-requests/{req_id}",
                    description=item.get("body", item.get("description", "")),
                    already_interested=item.get("already_interested", False),
                )
            )
        return requests
