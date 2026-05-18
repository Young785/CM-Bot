#!/usr/bin/env python3
"""
Codementor Bot - Hybrid Mode
Uses API for scanning (A2) and Browser for processing (A1)
Avoids re-login by using extracted tokens
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any
import aiohttp
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Support per-user configuration via environment variables
USER_ID = os.environ.get('USER_ID', '')
IS_ADMIN = os.environ.get('IS_ADMIN', 'false') == 'true'
USER_DATA_DIR = Path("user_data")

# A2 tokens are global (shared scanner account)
GLOBAL_TOKENS_FILE = Path("session_tokens.json")

if USER_ID:
    # Per-user mode (multi-user system)
    REQUESTS_DB = USER_DATA_DIR / f"{USER_ID}_requests.json"
    # A1 tokens per-user, A2 tokens global
    USER_TOKENS_FILE = USER_DATA_DIR / f"{USER_ID}_tokens.json"
    CONFIG_FILE = USER_DATA_DIR / f"{USER_ID}_bot_config.json"
    logger.info(f"Running in per-user mode for user: {USER_ID}")
else:
    # Legacy single-user mode
    REQUESTS_DB = Path("requests_db.json")
    USER_TOKENS_FILE = Path("session_tokens.json")
    GLOBAL_TOKENS_FILE = Path("session_tokens.json")
    CONFIG_FILE = Path("config.json")
    logger.info("Running in single-user mode")

API_BASE = "https://api.codementor.io"
REQUESTS_ENDPOINT = f"{API_BASE}/api/v2/requests/search?search_type=all"

DEFAULT_MESSAGE = "I am an experienced Software Engineer with many years experience in different stacks and i will like to show interest in your request"


def _load_json_dict(path: Path) -> Dict:
    """Read a JSON object from disk; tolerate missing, empty, or corrupt files."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("Ignoring non-object JSON in %s", path)
            return {}
        return data
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("Ignoring invalid JSON in %s: %s", path, e)
        return {}


@dataclass
class Request:
    request_id: str  # For display/storage (slug or id)
    random_key: str  # For API calls (full UUID)
    title: str
    author: str
    budget: str
    request_type: str = ""
    tags: list = None
    interested_count: int = 0
    posted_time: str = ""
    url: str = ""
    description: str = ""
    already_interested: bool = False
    processed_at: str = ""
    
    def __post_init__(self):
        if self.tags is None:
            self.tags = []
    
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, d): return cls(**d)


def load_tokens() -> Dict[str, Dict]:
    """Load tokens - A2 from global, A1 from user file"""
    tokens = {}
    
    # Load global A2 tokens
    global_tokens = _load_json_dict(GLOBAL_TOKENS_FILE)
    if global_tokens.get("A2"):
        tokens["A2"] = global_tokens["A2"]
        logger.info("Loaded A2 token from global file")
    
    # Load per-user A1 tokens
    user_tokens = _load_json_dict(USER_TOKENS_FILE)
    if user_tokens.get("A1"):
        tokens["A1"] = user_tokens["A1"]
        logger.info("Loaded A1 token from user file")
    
    return tokens


def save_tokens(tokens: Dict[str, Dict]):
    """Save tokens - A1 to user file, A2 to global file"""
    # Save A1 to user tokens file
    if USER_TOKENS_FILE:
        user_tokens = {}
        if 'A1' in tokens:
            user_tokens['A1'] = tokens['A1']
        try:
            USER_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(USER_TOKENS_FILE, 'w') as f:
                json.dump(user_tokens, f, indent=2)
            logger.info("Saved A1 token to user file")
        except Exception as e:
            logger.error(f"Failed to save user tokens: {e}")
    
    # Save A2 to global tokens file (only if admin or not in per-user mode)
    if IS_ADMIN or not USER_ID:
        try:
            global_tokens = _load_json_dict(GLOBAL_TOKENS_FILE)
            if "A2" in tokens:
                global_tokens["A2"] = tokens["A2"]
                GLOBAL_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(GLOBAL_TOKENS_FILE, "w", encoding="utf-8") as f:
                    json.dump(global_tokens, f, indent=2)
                logger.info("Saved A2 token to global file")
        except Exception as e:
            logger.error(f"Failed to save global tokens: {e}")


class CodementorAPI:
    """API client for scanning"""
    
    def __init__(self, tokens: Dict[str, Dict]):
        self.tokens = tokens
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def _get_headers_and_cookies(self, account: str) -> tuple:
        """Get headers and cookies for account"""
        acc = self.tokens.get(account, {})
        token = acc.get('access_token')
        if not token:
            return None, None
        
        headers = {
            'X-Requested-From': 'cm-web',
            'Origin': 'https://www.codementor.io',
            'Referer': 'https://www.codementor.io/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0'
        }
        
        cookies = {'ACCESS_TOKEN': token}
        
        return headers, cookies
    
    async def get_requests(self, account: str) -> List[Request]:
        """Get requests via API"""
        headers, cookies = self._get_headers_and_cookies(account)
        if not headers:
            return []
        
        try:
            async with self.session.get(REQUESTS_ENDPOINT, headers=headers, cookies=cookies) as resp:
                text = await resp.text()
                
                if resp.status == 200:
                    data = json.loads(text)
                    return self._parse_requests(data)
                elif resp.status == 401:
                    logger.error(f"Token expired for {account}")
                    return []
                else:
                    logger.error(f"API error: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Failed: {e}")
            return []
    
    def _parse_requests(self, data: Any) -> List[Request]:
        """Parse API response"""
        requests = []
        items = data if isinstance(data, list) else data.get('requests', data.get('data', []))
        
        for item in items:
            try:
                req_id = str(item.get('id', item.get('slug', '')))
                random_key = item.get('random_key', req_id)  # Use random_key for API
                if not req_id:
                    continue
                
                requests.append(Request(
                    request_id=req_id,
                    random_key=str(random_key),
                    title=item.get('title', 'Untitled'),
                    author=item.get('user', {}).get('username', 'Unknown'),
                    budget=str(item.get('estimated_budget', '')),
                    request_type=item.get('request_type', 'Unknown'),
                    tags=item.get('tag_list', []) or [],
                    interested_count=item.get('interest_count', 0),
                    posted_time=str(item.get('created_at', '')),
                    url=f"https://www.codementor.io/m/dashboard/open-requests/{req_id}",
                    description=item.get('body', ''),
                    already_interested=item.get('already_interested', False)
                ))
            except:
                continue
        
        return requests


class CodementorBot:
    """Main bot - API scanning + Browser processing"""
    
    def __init__(self):
        self.tokens = load_tokens()
        self.storage = self._load_storage()
        self.config = self._load_config()
        self.playwright = None
        self.browser = None
        self.a1_context = None
    
    def _load_config(self) -> Dict:
        defaults = {'message': DEFAULT_MESSAGE}
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                data = json.load(f)
                defaults['message'] = data.get('message', DEFAULT_MESSAGE)
                if 'account_a1' in data:
                    defaults['a1_email'] = data['account_a1'].get('email', '')
                    defaults['a1_password'] = data['account_a1'].get('password', '')
        return defaults
    
    def _load_storage(self) -> Dict[str, Request]:
        if REQUESTS_DB.exists():
            try:
                with open(REQUESTS_DB) as f:
                    content = f.read().strip()
                    if not content:
                        return {}
                    data = json.loads(content)
                    return {k: Request.from_dict(v) for k, v in data.items()}
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Could not load storage: {e}")
                return {}
        return {}
    
    def save_storage(self):
        with open(REQUESTS_DB, 'w') as f:
            json.dump({k: v.to_dict() for k, v in self.storage.items()}, f, indent=2)
    
    async def extract_token_interactive(self, account: str, email: str, headless: bool = False) -> Optional[Dict[str, str]]:
        """Open browser and extract both access and refresh tokens using persistent profile"""
        from playwright.async_api import async_playwright
        import os
        
        # Force headless if no display available (headless server)
        no_display = not os.environ.get('DISPLAY')
        
        # For A2: check if this is first login (no existing token) or refresh
        if account == 'A2':
            has_existing_token = bool(self.tokens.get('A2', {}).get('access_token', ''))
            # First login: visible, Refresh: headless
            is_headless = True if no_display else (headless if has_existing_token else False)
            mode_str = " [headless refresh]" if is_headless else " [first login - visible]"
        else:
            is_headless = True if no_display else False
            mode_str = " [headless - no display]" if no_display else ""
        
        logger.info(f"\n🔓 Opening Chrome browser for {account} ({email}){mode_str}")
        
        p = await async_playwright().start()
        
        # Use persistent user data directory to maintain login session
        user_data_dir = Path.home() / f".codementor_bot_profile_{account.lower()}"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Launch Chrome with persistent context with network fixes
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=is_headless,
                channel='chrome',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-web-security',
                    '--allow-running-insecure-content',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-site-isolation-trials',
                    '--disable-dev-shm-usage'
                ],
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            browser = None  # Persistent context has no browser property
        except Exception as e:
            logger.warning(f"Persistent context failed: {e}")
            # Fallback to regular launch with network fixes
            browser = await p.chromium.launch(
                headless=is_headless,
                channel='chrome',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-web-security'
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
        page = await context.new_page()
        
        tokens = {'access_token': None, 'refresh_token': None}
        
        try:
            # Navigate to dashboard
            await page.goto("https://www.codementor.io/m/dashboard/open-requests", timeout=60000)
            await asyncio.sleep(3)
            
            # Check if logged in - if not, wait for login to complete automatically
            login_attempts = 0
            while 'login' in page.url or 'arc.dev' in page.url:
                if is_headless:
                    # On headless server, can't do manual login - bail immediately
                    logger.error("  ✗ Session expired. Cannot login interactively on headless server.")
                    logger.error("  → Please update the token manually via the web UI (Tokens page).")
                    logger.error("  → Copy your ACCESS_TOKEN cookie from Codementor in your local browser.")
                    return None
                if login_attempts == 0:
                    logger.info("  ⚠ Not logged in. Please login manually in the browser window.")
                    logger.info("  ⏳ Waiting for you to complete login... (will auto-detect)")
                login_attempts += 1
                await asyncio.sleep(2)  # Check every 2 seconds
                if login_attempts > 150:  # 5 minutes timeout
                    logger.error("  ✗ Timeout waiting for login (5 minutes)")
                    return None
            
            # Give page time to fully load after login
            await asyncio.sleep(3)
            logger.info(f"  ✓ Page loaded: {page.url}")
            
            # Extract tokens from cookies
            logger.info("  🔍 Extracting tokens...")
            cookies = await context.cookies()
            
            for cookie in cookies:
                if cookie['name'] == 'ACCESS_TOKEN':
                    tokens['access_token'] = cookie['value']
                    logger.info(f"  ✓ Access token extracted: {tokens['access_token'][:30]}...")
                elif cookie['name'] == 'REFRESH_TOKEN':
                    tokens['refresh_token'] = cookie['value']
                    logger.info(f"  ✓ Refresh token extracted: {tokens['refresh_token'][:30]}...")
            
            if not tokens['access_token']:
                # Try from localStorage
                try:
                    tokens['access_token'] = await page.evaluate("""
                        () => {
                            const auth = localStorage.getItem('auth');
                            if (auth) {
                                const data = JSON.parse(auth);
                                return data.access_token || null;
                            }
                            return null;
                        }
                    """)
                    if tokens['access_token']:
                        logger.info(f"  ✓ Access token from localStorage: {tokens['access_token'][:30]}...")
                except:
                    pass
            
            return tokens if tokens['access_token'] else None
            
        except Exception as e:
            logger.error(f"  Extraction error: {e}")
            return None
        finally:
            try:
                await context.close()
            except:
                pass
            if browser:
                try:
                    await browser.close()
                except:
                    pass
            try:
                await p.stop()
            except:
                pass
    
    async def try_refresh_token(self, account: str) -> Optional[str]:
        """Try to refresh access token using refresh token cookie via headless browser"""
        acc_data = self.tokens.get(account, {})
        refresh_token = acc_data.get('refresh_token', '')
        
        if not refresh_token:
            logger.info(f"  No refresh token for {account}")
            return None
        
        try:
            logger.info(f"  Trying refresh token via headless browser for {account}...")
            from playwright.async_api import async_playwright
            
            p = await async_playwright().start()
            browser = await p.chromium.launch(
                headless=True,
                channel='chrome',
                args=['--no-sandbox', '--disable-web-security', '--disable-dev-shm-usage']
            )
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            # Set the refresh token cookie
            await context.add_cookies([{
                'name': 'REFRESH_TOKEN',
                'value': refresh_token,
                'domain': '.codementor.io',
                'path': '/',
            }])
            
            page = await context.new_page()
            await page.goto('https://www.codementor.io/m/dashboard/open-requests', timeout=30000)
            await asyncio.sleep(5)
            
            # Extract fresh tokens from cookies
            new_access = None
            new_refresh = None
            cookies = await context.cookies()
            for c in cookies:
                if c['name'] == 'ACCESS_TOKEN':
                    new_access = c['value']
                elif c['name'] == 'REFRESH_TOKEN':
                    new_refresh = c['value']
            
            await browser.close()
            await p.stop()
            
            if new_access:
                logger.info(f"  ✓ Token refreshed via headless browser")
                # Update refresh token too if it changed
                if new_refresh and new_refresh != refresh_token:
                    self.tokens.setdefault(account, {})['refresh_token'] = new_refresh
                    logger.info(f"  ✓ Refresh token also updated")
                return new_access
            else:
                logger.warning(f"  Headless browser refresh failed - no ACCESS_TOKEN in cookies")
                return None
                        
        except Exception as e:
            logger.warning(f"  Refresh token failed: {e}")
        
        return None
    
    async def ensure_valid_token(self, account: str, email: str) -> bool:
        """Ensure we have a valid token - try refresh API first, then browser only if no refresh token"""
        # Check current token
        acc_data = self.tokens.get(account, {})
        token = acc_data.get('access_token', '')
        
        if token:
            # Token exists - validate it
            logger.info(f"  Checking {account} token...")
            async with aiohttp.ClientSession() as session:
                headers = {
                    'X-Requested-From': 'cm-web',
                    'Origin': 'https://www.codementor.io',
                    'Referer': 'https://www.codementor.io/',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0'
                }
                cookies = {'ACCESS_TOKEN': token}
                
                try:
                    async with session.get(REQUESTS_ENDPOINT, headers=headers, cookies=cookies) as resp:
                        if resp.status == 200:
                            logger.info(f"  ✓ {account} token is valid")
                            return True
                        elif resp.status == 401:
                            logger.warning(f"  ⚠ {account} token expired (401)")
                            # Try refresh API first
                            new_token = await self.try_refresh_token(account)
                            if new_token:
                                self.tokens[account]['access_token'] = new_token
                                save_tokens(self.tokens)
                                logger.info(f"  ✓ {account} token refreshed via API")
                                return True
                            # Refresh API failed - will proceed to browser login below
                            logger.info(f"  Refresh API failed, will open browser for login...")
                            # Fall through to browser login
                        else:
                            logger.warning(f"  ⚠ {account} token check failed (status {resp.status})")
                            return False  # Some other error, don't open browser
                except Exception as e:
                    logger.warning(f"  ⚠ Token check failed: {e}")
                    return False
                
                # If we got 401 and refresh failed, fall through to browser login
                
        else:
            logger.info(f"  🔄 No token found for {account}")
        
        # Need to extract fresh token via browser
        # Skip browser login for A2 in per-user mode unless admin (A2 is admin-managed)
        if account == 'A2' and USER_ID and not IS_ADMIN:
            logger.error(f"  ✗ A2 token invalid or expired. Please contact admin to refresh A2 token.")
            return False
        
        import os
        no_display = not os.environ.get('DISPLAY')
        
        if no_display:
            logger.info(f"  🔄 Attempting headless browser token refresh for {account}...")
        else:
            logger.info(f"  🔄 Opening browser for {account} login...")
        
        token_data = await self.extract_token_interactive(account, email, headless=no_display)
        
        if token_data and token_data.get('access_token'):
            # Save both tokens
            self.tokens[account] = {
                'access_token': token_data['access_token'],
                'refresh_token': token_data.get('refresh_token', ''),
                'user_email': email
            }
            save_tokens(self.tokens)
            logger.info(f"  ✓ {account} tokens saved (access + refresh)")
            return True
        else:
            logger.error(f"  ✗ Failed to extract {account} token")
            return False
    
    async def init_browser(self):
        """Initialize browser for A1 only - uses Chrome with network fixes"""
        from playwright.async_api import async_playwright
        import tempfile
        
        self.playwright = await async_playwright().start()
        
        # Use temp user data dir to avoid conflicts
        temp_dir = tempfile.mkdtemp(prefix='chrome_a1_')
        
        try:
            # Try persistent context first with network fixes
            self.a1_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=False,
                channel='chrome',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-web-security',
                    '--allow-running-insecure-content',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-site-isolation-trials',
                    '--disable-dev-shm-usage'
                ],
                viewport={'width': 1280, 'height': 800}
            )
            self.browser = None  # Persistent context doesn't have separate browser
        except:
            # Fallback to regular launch with network fixes
            self.browser = await self.playwright.chromium.launch(
                headless=False,
                channel='chrome',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-web-security'
                ]
            )
            self.a1_context = await self.browser.new_context(
                viewport={'width': 1280, 'height': 800}
            )
        
        logger.info("Browser initialized for A1")
    
    async def close_browser(self):
        if self.a1_context:
            await self.a1_context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
    
    async def login_a1(self) -> bool:
        """Login A1 using browser"""
        email = self.config.get('a1_email', '')
        password = self.config.get('a1_password', '')
        
        if not email or not password:
            logger.error("A1 credentials not found in config.json")
            return False
        
        logger.info("\nLogging in A1...")
        page = await self.a1_context.new_page()
        
        try:
            await page.goto("https://www.codementor.io/login", timeout=60000)
            await asyncio.sleep(3)
            
            if 'dashboard' in page.url or 'open-requests' in page.url:
                logger.info("  ✓ A1 already logged in")
                return True
            
            await page.fill('input[type="email"]', email)
            await page.fill('input[type="password"]', password)
            await page.click('button[type="submit"]')
            
            await asyncio.sleep(5)
            
            if 'arc.dev' in page.url:
                logger.info("  Arc.dev login detected, waiting...")
                for _ in range(15):
                    await asyncio.sleep(2)
                    if 'codementor.io' in page.url and ('dashboard' in page.url or 'open-requests' in page.url):
                        break
            
            if 'dashboard' in page.url or 'open-requests' in page.url:
                logger.info("  ✓ A1 logged in successfully")
                return True
            else:
                logger.error(f"  ✗ Login failed: {page.url}")
                return False
                
        except Exception as e:
            logger.error(f"  Login error: {e}")
            return False
        finally:
            await page.close()
    
    async def process_request_a1_api(self, request: Request) -> bool:
        """Process a request with A1 using API endpoints (no browser)"""
        import uuid
        
        logger.info(f"\n  Processing via API: {request.title[:50]}...")
        
        a1_token = self.tokens.get('A1', {}).get('access_token', '')
        if not a1_token:
            logger.error("    ✗ No A1 token available")
            return False
        
        headers = {
            'X-Requested-From': 'cm-web',
            'Origin': 'https://www.codementor.io',
            'Referer': 'https://www.codementor.io/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json;charset=utf-8',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0'
        }
        
        cookies = {'ACCESS_TOKEN': a1_token}
        
        success = False
        
        async with aiohttp.ClientSession() as session:
            # Step 1: Express Interest
            # POST /api/v2/requests/{request_id}/interests
            interest_url = f"https://api.codementor.io/api/v2/requests/{request.random_key}/interests"
            message = self.config.get('message', DEFAULT_MESSAGE)
            
            interest_payload = {
                "message": message,
                "open_to_special_rate": False
            }
            
            try:
                logger.info(f"    1. Expressing interest...")
                async with session.post(
                    interest_url, 
                    headers=headers, 
                    cookies=cookies,
                    json=interest_payload
                ) as resp:
                    if resp.status in [200, 201]:
                        logger.info(f"    ✓ Interest expressed (status {resp.status})")
                        success = True
                    elif resp.status == 409:
                        logger.info(f"    ✓ Already expressed interest (409)")
                        success = True
                    else:
                        text = await resp.text()
                        logger.warning(f"    ⚠ Interest failed: {resp.status} - {text[:100]}")
            except Exception as e:
                logger.error(f"    ✗ Interest error: {e}")
            
            # Step 2: Get username from request data (already have it from A2 scan)
            username = request.author
            if username:
                logger.info(f"    2. Using author: @{username}")
            else:
                logger.warning(f"    ⚠ No author in request data")
            
            # Step 3: Send Message
            # POST /api/v2/chats/messages/{username}
            if username:
                try:
                    logger.info(f"    3. Sending message to @{username}...")
                    msg_url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
                    
                    temp_id = str(uuid.uuid4())
                    msg_payload = {
                        "message": {
                            "content": message,
                            "type": "message",
                            "request": {
                                "temp_message_id": temp_id
                            }
                        }
                    }
                    
                    async with session.post(
                        msg_url,
                        headers=headers,
                        cookies=cookies,
                        json=msg_payload
                    ) as resp:
                        if resp.status in [200, 201]:
                            logger.info(f"    ✓ Message sent (status {resp.status})")
                        else:
                            text = await resp.text()
                            logger.warning(f"    ⚠ Message failed: {resp.status} - {text[:100]}")
                except Exception as e:
                    logger.error(f"    ✗ Message error: {e}")
            else:
                logger.warning(f"    ⚠ Skipping message - no username available")
        
        return success
    
    async def run(self):
        """Main workflow - try using tokens first, only refresh on 401"""
        logger.info("\n" + "="*60)
        logger.info("CODEMENTOR BOT - HYBRID MODE")
        logger.info("="*60)
        
        a2_email = self.config.get('a2_email', 'kodaoluidris@gmail.com')
        a1_email = self.config.get('a1_email', 'tescointsite@gmail.com')
        
        # Step 1: Scan with A2 (try existing token first, refresh if 401)
        logger.info("\n[1/3] Scanning with A2 (API)...")
        a2_requests = await self.scan_with_retry('A2', a2_email)
        if a2_requests is None:
            logger.error("Failed to get A2 requests. Cannot continue.")
            return
        logger.info(f"Found {len(a2_requests)} requests from API")
        
        # Step 2: Get A1's current requests (try existing token first, refresh if 401)
        logger.info("\n[2/3] Getting A1's current requests (API)...")
        a1_requests = await self.scan_with_retry('A1', a1_email)
        if a1_requests is None:
            logger.error("Failed to get A1 requests. Cannot continue.")
            return
        a1_ids = {r.request_id for r in a1_requests if r.already_interested}
        logger.info(f"A1 has expressed interest in {len(a1_ids)} requests")
        
        # Find missing requests (A2 has them but A1 hasn't expressed interest)
        missing_requests = []
        for req in a2_requests:
            if req.request_id not in self.storage and req.request_id not in a1_ids:
                missing_requests.append(req)
        
        # Also check for new active requests (not in storage AND not already interested)
        active_new = [r for r in a2_requests if r.request_id not in self.storage and r.request_id not in a1_ids]
        
        to_process = list({r.request_id: r for r in missing_requests + active_new}.values())
        logger.info(f"\nMissing/Active requests to process: {len(to_process)}")
        
        if not to_process:
            logger.info("No new requests to process.")
            return
        
        # Step 3: Process with A1 via API (no browser)
        logger.info(f"\n[3/3] Processing {len(to_process)} requests with A1 (API)...")
        
        # Process each request using API endpoints
        for req in to_process:
            success = await self.process_request_a1_api(req)
            if success:
                req.processed_at = datetime.now().isoformat()
                self.storage[req.request_id] = req
                self.save_storage()
                logger.info("    ✓ Saved to database")
            await asyncio.sleep(2)  # Rate limiting between requests
        
        logger.info("\n" + "="*60)
        logger.info(f"Done! Total stored: {len(self.storage)}")
        logger.info("="*60 + "\n")
    
    async def scan_with_retry(self, account: str, email: str) -> Optional[list]:
        """Try scanning with existing token, refresh if 401, open browser if refresh fails"""
        # Try with current token
        result = await self.try_scan(account)
        if result is not None:
            return result
        
        # Got 401 - try to refresh/get valid token (will open browser if needed)
        logger.info(f"  Token failed for {account}, attempting refresh/browser login...")
        refreshed = await self.ensure_valid_token(account, email)
        
        if refreshed:
            # Reload tokens and retry
            self.tokens = load_tokens()
            result = await self.try_scan(account)
            if result is not None:
                return result
        
        # Browser login was cancelled or failed
        logger.error(f"  Failed to get valid token for {account}")
        return None
    
    async def try_scan(self, account: str) -> Optional[list]:
        """Try scanning with current token. Returns list on success, None on 401/failure"""
        # Ensure tokens is a dict - double check
        if not isinstance(self.tokens, dict):
            logger.error(f"  Tokens is not a dict: {type(self.tokens)}, reloading...")
            self.tokens = load_tokens()
        
        if not isinstance(self.tokens, dict):
            logger.error(f"  Still not a dict after reload: {type(self.tokens)}")
            return None
        
        # Safely get token
        try:
            acc_data = self.tokens.get(account, {})
            if isinstance(acc_data, list):
                logger.error(f"  Account data is a list: {acc_data}")
                return None
            token = acc_data.get('access_token', '')
        except AttributeError as e:
            logger.error(f"  Token access error: {e}, tokens type: {type(self.tokens)}")
            return None
        
        if not token:
            logger.info(f"  No token for {account}")
            return None
        
        headers = {
            'X-Requested-From': 'cm-web',
            'Origin': 'https://www.codementor.io',
            'Referer': 'https://www.codementor.io/',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0'
        }
        cookies = {'ACCESS_TOKEN': token}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(REQUESTS_ENDPOINT, headers=headers, cookies=cookies) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Debug: log the actual response structure
                        logger.info(f"  API response type: {type(data).__name__}, items: {len(data) if isinstance(data, (list, dict)) else 'N/A'}")
                        if isinstance(data, dict) and data:
                            logger.info(f"  Dict keys: {list(data.keys())[:5]}")
                        
                        # Handle both list and dict responses
                        if isinstance(data, list):
                            items = data
                            logger.debug(f"  Response is list with {len(items)} items")
                        elif isinstance(data, dict):
                            items = data.get('data', [])
                            if not items:
                                # Try other common keys
                                items = data.get('requests', data.get('results', []))
                            logger.debug(f"  Response is dict, found {len(items)} items in data/requests/results")
                        else:
                            logger.error(f"  Unexpected response type: {type(data)}")
                            return None
                        
                        requests = []
                        for i, item in enumerate(items):
                            if not isinstance(item, dict):
                                logger.debug(f"  Item {i} is not a dict: {type(item)}")
                                continue
                            req_id = item.get('id', item.get('slug', ''))
                            random_key = item.get('random_key', req_id)
                            # Use random_key as req_id if id/slug are missing
                            if not req_id and random_key:
                                req_id = random_key
                            # Debug first few items
                            if i < 3:
                                logger.info(f"  Item {i} keys: {list(item.keys())[:10]}, id={req_id}, random_key={random_key}")
                            if not req_id:
                                logger.debug(f"  Item {i} has no id, slug, or random_key")
                                continue
                            
                            requests.append(Request(
                                request_id=str(req_id),
                                random_key=str(random_key),
                                title=item.get('title', ''),
                                author=item.get('user', {}).get('username', item.get('author', 'Unknown')),
                                budget=str(item.get('budget', item.get('estimated_budget', 0))),
                                request_type=item.get('type', item.get('request_type', '')),
                                tags=item.get('tag_list', item.get('tags', [])) or [],
                                interested_count=item.get('interest_count', 0),
                                posted_time=str(item.get('created_at', '')),
                                url=f"https://www.codementor.io/m/dashboard/open-requests/{req_id}",
                                description=item.get('body', item.get('description', '')),
                                already_interested=item.get('already_interested', False)
                            ))
                        
                        logger.info(f"  Parsed {len(requests)} requests from API")
                        return requests
                    elif resp.status == 401:
                        logger.warning(f"  {account} token expired (401)")
                        return None
                    else:
                        logger.warning(f"  {account} scan failed: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"  {account} scan error: {e}")
            return None


async def main():
    bot = CodementorBot()
    await bot.run()


if __name__ == '__main__':
    try:
        import aiohttp
        from playwright.async_api import async_playwright
    except ImportError:
        print("Installing dependencies...")
        os.system(".venv/bin/pip install aiohttp playwright")
        os.system(".venv/bin/playwright install chromium")
        exit(1)
    
    asyncio.run(main())
