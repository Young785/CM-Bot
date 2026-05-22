#!/usr/bin/env python3
"""
Refresh Codementor tokens using stored credentials (no manual cookie paste).

Usage:
  cd /root/CMBOTv2
  .venv/bin/python scripts/refresh_auth.py              # A1 + A2 (single-user)
  USER_ID=abc123 .venv/bin/python scripts/refresh_auth.py  # per-user A1 only
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cmbot.auth.service import TokenAuthService


async def _main():
    service = TokenAuthService()
    only = os.environ.get("REFRESH_ACCOUNTS", "").strip()
    if only:
        accounts = [a.strip() for a in only.split(",") if a.strip()]
    else:
        accounts = ["A1"]
        if os.environ.get("IS_ADMIN", "").lower() == "true" or not os.environ.get("USER_ID"):
            accounts.append("A2")
    print(f"[auth] Refreshing accounts: {', '.join(accounts)}", flush=True)
    results = {}
    for account in accounts:
        email, _ = service.credentials_for(account)
        print(f"[auth] {account} ({email or 'no email'})...", flush=True)
        ok = await service.ensure_valid_token(account, email)
        results[account] = ok
        print(f"[auth] {account}: {'OK' if ok else 'FAILED'}", flush=True)
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
