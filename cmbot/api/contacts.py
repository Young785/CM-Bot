"""Fetch all Codementor chat contacts with pagination."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

CONTACTS_HEADERS = {
    "X-Requested-From": "cm-chat",
    "x-custom-referrer": "https://www.codementor.io/m/dashboard/open-requests",
    "Origin": "https://www.codementor.io",
    "Referer": "https://www.codementor.io/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
}


async def fetch_all_contacts(
    access_token: str,
    *,
    max_pages: int = 100,
    page_size_hint: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Paginate contacts API using before_timestamp until exhausted.
    Returns (all_contacts, pages_fetched).
    """
    cookies = {"ACCESS_TOKEN": access_token}
    all_contacts: List[Dict[str, Any]] = []
    before_timestamp = int(time.time())
    pages = 0

    async with aiohttp.ClientSession() as session:
        for _ in range(max_pages):
            url = (
                "https://api.codementor.io/api/v2/chats/contacts"
                f"?before_timestamp={before_timestamp}"
            )
            async with session.get(
                url,
                headers=CONTACTS_HEADERS,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status == 401:
                    raise PermissionError("Token expired (401)")
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Contacts API {resp.status}: {text[:120]}")

                batch = await resp.json()
                if not isinstance(batch, list) or not batch:
                    break

                pages += 1
                all_contacts.extend(batch)

                timestamps = [
                    c.get("last_message_at")
                    for c in batch
                    if isinstance(c, dict) and c.get("last_message_at")
                ]
                if not timestamps:
                    break

                oldest = min(timestamps)
                if oldest >= before_timestamp:
                    break
                before_timestamp = int(oldest) - 1

                if len(batch) < page_size_hint:
                    break

    return all_contacts, pages
