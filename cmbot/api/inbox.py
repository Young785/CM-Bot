"""Codementor inbox: contacts + last message preview + unread detection."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from cmbot.api.contacts import CONTACTS_HEADERS, fetch_all_contacts

CHAT_HEADERS = {
    **CONTACTS_HEADERS,
    "X-Requested-From": "cm-web",
    "Content-Type": "application/json;charset=utf-8",
}


def mentor_username_from_token(access_token: str) -> str:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("username") or ""
    except (IndexError, json.JSONDecodeError, ValueError):
        return ""


def _normalize_message(msg: Dict[str, Any], mentor_username: str) -> Dict[str, Any]:
    sender = msg.get("sender") or {}
    sender_username = sender.get("username") or ""
    is_me = sender.get("role") == "mentor" or (
        mentor_username and sender_username == mentor_username
    )
    return {
        "id": msg.get("id"),
        "content": msg.get("content") or "",
        "type": msg.get("type") or "message",
        "created_at": msg.get("created_at"),
        "from_me": is_me,
        "sender_name": sender.get("name") or sender_username,
        "sender_username": sender_username,
        "sender_avatar": sender.get("small_avatar_url") or "",
    }


async def fetch_thread(
    access_token: str,
    username: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Full conversation with a user.
    Returns (thread_dict, error_message). Messages oldest-first for chat UI.
    """
    url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
    cookies = {"ACCESS_TOKEN": access_token}
    mentor = mentor_username_from_token(access_token)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=CHAT_HEADERS,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status == 401:
                    return None, "Token expired (401)"
                if resp.status != 200:
                    text = await resp.text()
                    return None, f"Thread load failed ({resp.status}): {text[:120]}"
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, str(e)

    opponent = data.get("opponent") or {}
    raw_messages = data.get("messages") or []
    messages = [_normalize_message(m, mentor) for m in reversed(raw_messages)]

    return {
        "username": opponent.get("username") or username,
        "name": opponent.get("name") or username,
        "small_avatar_url": opponent.get("small_avatar_url") or "",
        "online_status": opponent.get("online_status"),
        "messages": messages,
        "chat_url": f"https://www.codementor.io/messages/{username}",
    }, None


async def enrich_contacts(
    access_token: str,
    contacts: List[Dict[str, Any]],
    *,
    mentor_username: str,
    max_enrich: int = 250,
    concurrency: int = 12,
) -> List[Dict[str, Any]]:
    """Add preview + unread to the most recent contacts."""
    sorted_contacts = sorted(
        contacts,
        key=lambda c: c.get("last_message_at") or 0,
        reverse=True,
    )
    to_enrich = sorted_contacts[:max_enrich]
    rest = sorted_contacts[max_enrich:]

    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, Dict[str, Any]] = {}

    async with aiohttp.ClientSession() as session:

        async def _one(contact: Dict[str, Any]) -> None:
            uname = contact.get("username")
            if not uname:
                return
            async with sem:
                head = await fetch_thread_head_fast(session, access_token, uname)
            if head:
                results[uname] = {
                    **head,
                    "unread": not head["preview_from_me"],
                }

        await asyncio.gather(*[_one(c) for c in to_enrich])

    inbox: List[Dict[str, Any]] = []
    for c in sorted_contacts:
        uname = c.get("username") or ""
        row = {
            "username": uname,
            "name": c.get("name") or uname,
            "small_avatar_url": c.get("small_avatar_url") or "",
            "online_status": c.get("online_status") or "offline",
            "last_message_at": c.get("last_message_at"),
            "preview": "",
            "preview_at": None,
            "preview_from_me": False,
            "unread": False,
            "chat_url": f"https://www.codementor.io/messages/{uname}" if uname else "",
            "_source_account": c.get("_source_account"),
        }
        if uname in results:
            row.update(results[uname])
        inbox.append(row)

    return inbox


async def fetch_inbox(
    access_token: str,
    *,
    max_enrich: int = 250,
) -> Tuple[List[Dict[str, Any]], int]:
    contacts, pages = await fetch_all_contacts(access_token)
    mentor = mentor_username_from_token(access_token)
    inbox = await enrich_contacts(
        access_token,
        contacts,
        mentor_username=mentor,
        max_enrich=max_enrich,
    )
    return inbox, pages


async def mark_thread_read(access_token: str, username: str) -> bool:
    url = f"https://api.codementor.io/api/v2/chats/messages/{username}/read"
    cookies = {"ACCESS_TOKEN": access_token}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers=CHAT_HEADERS,
            cookies=cookies,
            json={},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return resp.status in (200, 201, 204)


async def fetch_thread_head_fast(
    session: aiohttp.ClientSession,
    access_token: str,
    username: str,
) -> Optional[Dict[str, Any]]:
    """Latest message only (single GET, no full normalize)."""
    url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
    cookies = {"ACCESS_TOKEN": access_token}
    try:
        async with session.get(
            url,
            headers=CHAT_HEADERS,
            cookies=cookies,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            messages = data.get("messages") or []
            if not messages:
                return None
            msg = messages[0]
            sender = msg.get("sender") or {}
            return {
                "preview": (msg.get("content") or "")[:200],
                "preview_at": msg.get("created_at"),
                "preview_from_me": sender.get("role") == "mentor",
                "sender_name": sender.get("name") or sender.get("username") or "",
            }
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def mark_all_read(access_token: str, usernames: List[str]) -> int:
    sem = asyncio.Semaphore(15)

    async def _one(session: aiohttp.ClientSession, user: str) -> bool:
        async with sem:
            return await mark_thread_read(access_token, user)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_one(session, u) for u in usernames if u],
            return_exceptions=True,
        )
    return sum(1 for r in results if r is True)
