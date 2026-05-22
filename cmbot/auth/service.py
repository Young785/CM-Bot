"""Automated Codementor authentication: validate, refresh, and extract tokens."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Optional

import aiohttp

from cmbot.auth.tokens import load_tokens, save_tokens
from cmbot.paths import GLOBAL_CONFIG_FILE, IS_ADMIN, REQUESTS_ENDPOINT, USER_ID

logger = logging.getLogger(__name__)

CM_HEADERS = {
    "X-Requested-From": "cm-web",
    "Origin": "https://www.codementor.io",
    "Referer": "https://www.codementor.io/",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
}


class TokenAuthService:
    """Handles token lifecycle without manual cookie copy when credentials exist."""

    def __init__(self, tokens: Optional[Dict[str, Dict]] = None, config: Optional[Dict] = None):
        self.tokens = tokens if tokens is not None else load_tokens()
        self.config = config or self._load_config()

    def _load_config(self) -> Dict:
        from cmbot.paths import CONFIG_FILE
        from cmbot.storage.json_store import load_json
        from cmbot.users import get_user_credentials

        defaults: Dict = {}
        if GLOBAL_CONFIG_FILE.exists():
            data = load_json(GLOBAL_CONFIG_FILE, {})
            if isinstance(data, dict):
                defaults["a2_email"] = data.get("account_a2", {}).get("email", "")
                defaults["a2_password"] = data.get("account_a2", {}).get("password", "")
                defaults["message"] = data.get("message", "")
        if CONFIG_FILE.exists():
            data = load_json(CONFIG_FILE, {})
            if isinstance(data, dict):
                defaults["a1_email"] = data.get("a1_email") or data.get("account_a1", {}).get("email", "")
                defaults["a1_password"] = data.get("a1_password") or data.get("account_a1", {}).get("password", "")
                defaults["a2_email"] = defaults.get("a2_email") or data.get("a2_email", "")
                defaults["a2_password"] = defaults.get("a2_password") or data.get("a2_password", "")
                defaults["message"] = defaults.get("message") or data.get("message", "")
        if USER_ID:
            creds = get_user_credentials(USER_ID)
            if creds.get("a1_email"):
                defaults["a1_email"] = creds["a1_email"]
            if creds.get("a1_password"):
                defaults["a1_password"] = creds["a1_password"]
            if creds.get("message"):
                defaults["message"] = creds["message"]
        return defaults

    def credentials_for(self, account: str) -> tuple[str, str]:
        key = account.lower()
        return (
            self.config.get(f"{key}_email", ""),
            self.config.get(f"{key}_password", ""),
        )

    async def validate_token(self, account: str) -> bool:
        acc_data = self.tokens.get(account, {})
        token = acc_data.get("access_token", "")
        if not token:
            return False
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    REQUESTS_ENDPOINT,
                    headers=CM_HEADERS,
                    cookies={"ACCESS_TOKEN": token},
                ) as resp:
                    return resp.status == 200
            except Exception as e:
                logger.warning("Token check failed for %s: %s", account, e)
                return False

    async def try_refresh_token(self, account: str) -> Optional[str]:
        acc_data = self.tokens.get(account, {})
        refresh_token = acc_data.get("refresh_token", "")
        if not refresh_token:
            logger.info("No refresh token for %s", account)
            return None

        try:
            from playwright.async_api import async_playwright

            logger.info("Refreshing %s via headless browser...", account)
            print(f"[auth] Refreshing {account} via refresh cookie...", flush=True)
            p = await async_playwright().start()
            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=["--no-sandbox", "--disable-web-security", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await context.add_cookies(
                [
                    {
                        "name": "REFRESH_TOKEN",
                        "value": refresh_token,
                        "domain": ".codementor.io",
                        "path": "/",
                    }
                ]
            )
            page = await context.new_page()
            await page.goto("https://www.codementor.io/m/dashboard/open-requests", timeout=30000)
            await asyncio.sleep(5)

            new_access = None
            new_refresh = None
            for c in await context.cookies():
                if c["name"] == "ACCESS_TOKEN":
                    new_access = c["value"]
                elif c["name"] == "REFRESH_TOKEN":
                    new_refresh = c["value"]

            await browser.close()
            await p.stop()

            if new_access:
                self.tokens.setdefault(account, {})["access_token"] = new_access
                if new_refresh:
                    self.tokens[account]["refresh_token"] = new_refresh
                save_tokens(self.tokens)
                return new_access
        except Exception as e:
            logger.warning("Refresh failed for %s: %s", account, e)
        return None

    async def _fill_login_form(self, page, email: str, password: str) -> bool:
        """Try common Codementor / Arc.dev login field selectors."""
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[id="email"]',
            'input[autocomplete="email"]',
        ]
        password_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[id="password"]',
        ]
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Sign in")',
            'button:has-text("Log in")',
            'button:has-text("Continue")',
        ]
        for sel in email_selectors:
            try:
                if await page.locator(sel).count():
                    await page.fill(sel, email)
                    break
            except Exception:
                continue
        else:
            return False
        for sel in password_selectors:
            try:
                if await page.locator(sel).count():
                    await page.fill(sel, password)
                    break
            except Exception:
                continue
        else:
            return False
        for sel in submit_selectors:
            try:
                if await page.locator(sel).count():
                    await page.locator(sel).first.click()
                    return True
            except Exception:
                continue
        return False

    async def _automated_login(self, page, email: str, password: str) -> bool:
        """Fill login form when credentials are configured."""
        if not email or not password:
            return False
        try:
            await page.goto("https://www.codementor.io/login", timeout=60000)
            await asyncio.sleep(2)
            if self._url_is_authenticated(page.url):
                return True
            if not await self._fill_login_form(page, email, password):
                return False
            await asyncio.sleep(5)
            if "arc.dev" in page.url:
                await self._fill_login_form(page, email, password)
                await asyncio.sleep(5)
            for _ in range(25):
                if self._url_is_authenticated(page.url):
                    return True
                await asyncio.sleep(2)
            return self._url_is_authenticated(page.url)
        except Exception as e:
            logger.warning("Automated login failed: %s", e)
            return False

    @staticmethod
    def _url_is_authenticated(url: str) -> bool:
        return "codementor.io" in url and (
            "dashboard" in url or "open-requests" in url or "/m/" in url
        )

    async def _read_tokens_from_context(self, context) -> Dict[str, Optional[str]]:
        tokens = {"access_token": None, "refresh_token": None}
        for cookie in await context.cookies():
            if cookie["name"] == "ACCESS_TOKEN":
                tokens["access_token"] = cookie["value"]
            elif cookie["name"] == "REFRESH_TOKEN":
                tokens["refresh_token"] = cookie["value"]
        return tokens

    def _has_refresh_token(self, account: str) -> bool:
        return bool(self.tokens.get(account, {}).get("refresh_token"))

    async def extract_tokens(
        self, account: str, email: str, headless: bool = False
    ) -> Optional[Dict[str, str]]:
        from playwright.async_api import async_playwright

        no_display = not os.environ.get("DISPLAY")
        cred_email, cred_password = self.credentials_for(account)
        profile_only = headless and not self._has_refresh_token(account)

        if account == "A2":
            has_existing = bool(self.tokens.get("A2", {}).get("access_token"))
            is_headless = True if no_display else (headless if has_existing else False)
        else:
            is_headless = True if no_display else headless

        logger.info("Extracting %s tokens (headless=%s)", account, is_headless)
        print(f"[auth] Opening browser for {account} (headless={is_headless})...", flush=True)
        p = await async_playwright().start()
        profile_dir = Path.home() / f".codementor_bot_profile_{account.lower()}"
        profile_dir.mkdir(parents=True, exist_ok=True)

        context = None
        browser = None
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=is_headless,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                viewport={"width": 1280, "height": 800},
            )
        except Exception:
            browser = await p.chromium.launch(headless=is_headless, channel="chrome", args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 800})

        page = await context.new_page()
        tokens = {"access_token": None, "refresh_token": None}

        try:
            # Persistent profile may already be logged in
            await page.goto("https://www.codementor.io/m/dashboard/open-requests", timeout=60000)
            await asyncio.sleep(3)
            tokens = await self._read_tokens_from_context(context)
            if tokens.get("access_token"):
                logger.info("Reused existing %s session from browser profile", account)
                return tokens

            if profile_only:
                print(
                    f"[auth] {account}: no REFRESH_TOKEN — headless login not supported. "
                    "Paste full document.cookie on the Tokens page (needs REFRESH_TOKEN).",
                    flush=True,
                )
                return None

            if cred_email and cred_password and not is_headless:
                logger.info("Logging in %s with stored credentials...", account)
                if not await self._automated_login(page, cred_email, cred_password):
                    await page.goto("https://www.codementor.io/m/dashboard/open-requests", timeout=60000)
                    await asyncio.sleep(3)

            attempts = 0
            while "login" in page.url or "arc.dev" in page.url:
                if is_headless:
                    print(
                        f"[auth] {account}: login required but headless OAuth cannot complete.",
                        flush=True,
                    )
                    return None
                if attempts == 0:
                    logger.info("Waiting for %s login in browser...", account)
                attempts += 1
                await asyncio.sleep(2)
                if attempts > 150:
                    logger.error("Login timeout for %s", account)
                    return None

            await asyncio.sleep(2)
            tokens = await self._read_tokens_from_context(context)

            if not tokens["access_token"]:
                try:
                    tokens["access_token"] = await page.evaluate(
                        """() => {
                            const auth = localStorage.getItem('auth');
                            if (auth) {
                                const data = JSON.parse(auth);
                                return data.access_token || null;
                            }
                            return null;
                        }"""
                    )
                except Exception:
                    pass

            return tokens if tokens["access_token"] else None
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            await p.stop()

    async def ensure_valid_token(self, account: str, email: str = "") -> bool:
        acc_data = self.tokens.get(account, {})
        if not acc_data.get("access_token"):
            logger.info("No %s token on file — obtaining session...", account)
        elif await self.validate_token(account):
            logger.info("%s token is valid", account)
            return True

        if acc_data.get("access_token"):
            new_token = await self.try_refresh_token(account)
            if new_token:
                self.tokens = load_tokens()
                return True

        if account == "A2" and USER_ID and not IS_ADMIN:
            logger.error("A2 token expired — contact admin to refresh shared scanner token.")
            return False

        email = email or self.credentials_for(account)[0]
        no_display = not os.environ.get("DISPLAY")
        headless = no_display or True

        if headless and not self._has_refresh_token(account):
            print(
                f"[auth] {account}: skipping Playwright login (no REFRESH_TOKEN on file).",
                flush=True,
            )
            try:
                token_data = await asyncio.wait_for(
                    self.extract_tokens(account, email, headless=True),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                print(f"[auth] {account}: profile check timed out (30s)", flush=True)
                token_data = None
        else:
            try:
                token_data = await asyncio.wait_for(
                    self.extract_tokens(account, email, headless=headless),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                print(f"[auth] {account}: token extraction timed out (90s)", flush=True)
                token_data = None

        if token_data and token_data.get("access_token"):
            self.tokens[account] = {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "user_email": email,
            }
            save_tokens(self.tokens)
            logger.info("%s tokens saved", account)
            return True

        logger.error("Failed to obtain valid token for %s", account)
        return False

    async def refresh_accounts(self, accounts: Optional[list[str]] = None) -> Dict[str, bool]:
        """Refresh all listed accounts (default A1 + A2 when permitted)."""
        targets = accounts or []
        if not targets:
            targets = ["A1"]
            if not USER_ID or IS_ADMIN:
                targets.append("A2")

        results = {}
        for acc in targets:
            email, _ = self.credentials_for(acc)
            results[acc] = await self.ensure_valid_token(acc, email)
        return results
