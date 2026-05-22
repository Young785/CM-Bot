"""Codementor chat / interest API helpers."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional, Tuple

import aiohttp

CM_HEADERS = {
    "X-Requested-From": "cm-web",
    "Origin": "https://www.codementor.io",
    "Referer": "https://www.codementor.io/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json;charset=utf-8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
}


async def send_chat_message(
    access_token: str, username: str, content: str
) -> Tuple[bool, str]:
    if not username:
        return False, "Username required"
    cookies = {"ACCESS_TOKEN": access_token}
    url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
    payload = {
        "message": {
            "content": content,
            "type": "message",
            "request": {"temp_message_id": str(uuid.uuid4())},
        }
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=CM_HEADERS, cookies=cookies, json=payload) as resp:
            if resp.status in (200, 201):
                return True, "Message sent"
            text = await resp.text()
            return False, f"Send failed ({resp.status}): {text[:120]}"


async def express_interest(
    access_token: str, random_key: str, message: str
) -> Tuple[bool, str]:
    cookies = {"ACCESS_TOKEN": access_token}
    url = f"https://api.codementor.io/api/v2/requests/{random_key}/interests"
    payload = {"message": message, "open_to_special_rate": False}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=CM_HEADERS, cookies=cookies, json=payload
        ) as resp:
            if resp.status in (200, 201, 409):
                return True, "Interest expressed"
            text = await resp.text()
            return False, f"Interest failed ({resp.status}): {text[:120]}"
