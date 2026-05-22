#!/usr/bin/env python3
"""
Codementor Bot Web UI - Multi-User Version
Manage the bot through a web interface with user authentication
"""

import json
import os
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import threading
from collections import defaultdict
import secrets
import time
import aiohttp

from cmbot.auth import TokenAuthService, load_merged_tokens
from cmbot.users import get_user_credentials
from cmbot.storage.json_store import (
    load_json as load_json_file,
    save_json as save_json_file,
    extract_access_token as extract_token_from_cookie_string,
    extract_refresh_token,
)
from werkzeug.middleware.proxy_fix import ProxyFix

SECRET_KEY_FILE = Path(__file__).resolve().parent / "user_data" / ".flask_secret_key"


def _load_secret_key() -> str:
    """Stable secret across PM2 restarts (random key on each restart logs everyone out)."""
    env_key = (os.environ.get("SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        SECRET_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes"),
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes"),
)

# File paths
TOKENS_FILE = Path("session_tokens.json")
CONFIG_FILE = Path("config.json")
REQUESTS_DB = Path("requests_db.json")
LOG_FILE = Path("bot_ui.log")
USERS_FILE = Path("users.json")
USER_DATA_DIR = Path("user_data")

# Create user data directory
USER_DATA_DIR.mkdir(exist_ok=True)

# Bot process tracking
bot_processes = {}  # user_id -> {process, running, status, message}
bot_locks = defaultdict(threading.Lock)
bot_schedulers = {}  # user_id -> BackgroundScheduler
global_bot_status = {"running": False, "message": "Ready"}


def reconcile_bot_process(user_id: str) -> None:
    """Clear stale running flags when the subprocess has exited."""
    info = bot_processes.get(user_id)
    if not info:
        return
    proc = info.get("process")
    if proc is not None and proc.poll() is not None:
        info["running"] = False
        status = info.get("status") or {}
        status["running"] = False
        info["status"] = status


def is_bot_process_alive(user_id: str) -> bool:
    reconcile_bot_process(user_id)
    proc = bot_processes.get(user_id, {}).get("process")
    return proc is not None and proc.poll() is None


def get_bot_status_for_user(user_id: str) -> dict:
    reconcile_bot_process(user_id)
    return bot_processes.get(user_id, {}).get(
        "status", {"running": False, "message": "Ready", "last_run": None}
    )


def _tokens_recently_validated(user_id: str, max_hours: int = 12) -> bool:
    data = load_json_file(USER_DATA_DIR / f"{user_id}_data.json", {})
    ts = data.get("tokens_validated_at")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts.replace("Z", ""))
        return datetime.now() - last < timedelta(hours=max_hours)
    except (ValueError, TypeError):
        return False


def _mark_tokens_validated(user_id: str) -> None:
    data_file = USER_DATA_DIR / f"{user_id}_data.json"
    data = load_json_file(data_file, {})
    data["tokens_validated_at"] = datetime.now().isoformat()
    save_json_file(data_file, data)


def _a1_access_from_storage(user_tokens: dict) -> str:
    """Extract bare ACCESS_TOKEN from merged user token file."""
    a1 = user_tokens.get("A1")
    if isinstance(a1, dict):
        raw = a1.get("access_token") or ""
    elif isinstance(a1, str):
        raw = a1
    else:
        raw = user_tokens.get("access_token") or ""
    if not raw:
        return ""
    return extract_token_from_cookie_string(raw) or str(raw).strip()


def get_a1_access_token(user_id: str, *, refresh_if_needed: bool = True) -> tuple[str | None, str | None]:
    """Return A1 access token; skip Playwright when recently validated."""
    path = USER_DATA_DIR / f"{user_id}_tokens.json"
    access = _a1_access_from_storage(load_merged_tokens(path))
    if access and _tokens_recently_validated(user_id):
        return access, None
    if access and not refresh_if_needed:
        return access, None
    if not refresh_if_needed:
        if access:
            return access, None
        return None, "A1 token not configured. Update tokens on the Tokens page."
    return get_valid_a1_token(user_id)


def _set_scheduler_persisted(user_id: str, enabled: bool, interval_minutes: int = 5) -> None:
    users = load_json_file(USERS_FILE, {})
    if user_id in users:
        users[user_id]["scheduler_enabled"] = enabled
        users[user_id]["scheduler_interval"] = interval_minutes
        save_json_file(USERS_FILE, users)
    data_file = USER_DATA_DIR / f"{user_id}_data.json"
    data = load_json_file(data_file, {})
    data["scheduler_enabled"] = enabled
    data["scheduler_interval"] = interval_minutes
    save_json_file(data_file, data)


def _ensure_scheduler_running(user_id: str):
    """Restore or return active scheduler for user."""
    if user_id in bot_schedulers and bot_schedulers[user_id].running:
        return bot_schedulers[user_id]
    users = load_json_file(USERS_FILE, {})
    u = users.get(user_id, {})
    if not u.get("scheduler_enabled") or not u.get("onboarding_complete"):
        return None
    interval = u.get("scheduler_interval", 5)
    sched = BackgroundScheduler(user_id, interval)
    if sched.start():
        bot_schedulers[user_id] = sched
        return sched
    return None


def get_valid_a1_token(user_id: str) -> tuple[str | None, str | None]:
    """Validate or refresh A1; returns (access_token, error_message)."""
    creds = get_user_credentials(user_id)
    old_env = {k: os.environ.get(k) for k in (
        "USER_ID", "IS_ADMIN", "USER_TOKENS_FILE", "USER_REQUESTS_FILE", "USER_CONFIG_FILE"
    )}
    try:
        for k, v in _bot_env_for_user(user_id, creds.get("is_admin", False)).items():
            if k != "PYTHONUNBUFFERED":
                os.environ[k] = v
        service = TokenAuthService()

        async def _go():
            if await service.validate_token("A1"):
                return True
            return await service.ensure_valid_token("A1", creds.get("a1_email", ""))

        if asyncio.run(asyncio.wait_for(_go(), timeout=90)):
            _mark_tokens_validated(user_id)
            tok = load_merged_tokens(USER_DATA_DIR / f"{user_id}_tokens.json")
            access = _a1_access_from_storage(tok)
            if access:
                return access, None
        return None, "A1 token invalid. Update tokens on the Tokens page."
    except asyncio.TimeoutError:
        return None, "Token refresh timed out"
    except Exception as e:
        return None, str(e)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def append_user_log(user_id: str, message: str) -> None:
    """Append a line to the user's log file (flushed immediately for live UI)."""
    user_log = USER_DATA_DIR / f"{user_id}.log"
    line = message if message.endswith("\n") else f"{message}\n"
    if not line.startswith("20") and " - " not in line[:30]:
        line = f"{datetime.now().isoformat()} - {line}"
    with open(user_log, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def _bot_env_for_user(user_id: str, is_admin: bool) -> dict:
    env = os.environ.copy()
    env["USER_ID"] = user_id
    env["IS_ADMIN"] = "true" if is_admin else "false"
    env["USER_TOKENS_FILE"] = str(USER_DATA_DIR / f"{user_id}_tokens.json")
    env["USER_REQUESTS_FILE"] = str(USER_DATA_DIR / f"{user_id}_requests.json")
    env["USER_CONFIG_FILE"] = str(USER_DATA_DIR / f"{user_id}_bot_config.json")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _quick_token_check(user_id: str, is_admin: bool) -> dict[str, bool]:
    """Fast HTTP validation — no Playwright."""
    old_env = {k: os.environ.get(k) for k in (
        "USER_ID", "IS_ADMIN", "USER_TOKENS_FILE", "USER_REQUESTS_FILE", "USER_CONFIG_FILE"
    )}
    try:
        for k, v in _bot_env_for_user(user_id, is_admin).items():
            if k != "PYTHONUNBUFFERED":
                os.environ[k] = v
        service = TokenAuthService()

        async def _check():
            accounts = ["A1"]
            if is_admin:
                accounts.append("A2")
            out = {}
            for acc in accounts:
                out[acc] = await service.validate_token(acc)
            return out

        return asyncio.run(asyncio.wait_for(_check(), timeout=20))
    except Exception as e:
        append_user_log(user_id, f"Token check error: {e}")
        return {"A1": False, "A2": False}
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _stream_subprocess_to_log(
    user_id: str,
    cmd: list[str],
    env: dict,
    timeout_sec: int = 120,
    label: str = "process",
) -> int:
    """Run a subprocess and stream stdout/stderr to the user log in real time."""
    append_user_log(user_id, f"Starting {label}...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(Path(__file__).resolve().parent),
    )
    user_log = USER_DATA_DIR / f"{user_id}.log"
    start = time.time()
    with open(user_log, "a", encoding="utf-8") as f:
        for line in iter(proc.stdout.readline, ""):
            if time.time() - start > timeout_sec:
                append_user_log(user_id, f"{label} timed out after {timeout_sec}s — continuing...")
                proc.kill()
                break
            if line:
                f.write(line)
                f.flush()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    append_user_log(user_id, f"{label} finished (exit {proc.returncode})")
    return proc.returncode or 0


def execute_bot_cycle(user_id: str, log_header: str | None = None) -> tuple[bool, str]:
    """
    Auth refresh + one bot subprocess. Serialized per user.
    Returns (success, reason) where reason is 'ok', 'busy', or 'error'.
    """
    with bot_locks[user_id]:
        reconcile_bot_process(user_id)
        if is_bot_process_alive(user_id):
            return False, "busy"

        users = load_json_file(USERS_FILE, {})
        user_config_file = create_user_config_file(user_id)
        user_log = USER_DATA_DIR / f"{user_id}.log"
        is_admin = users.get(user_id, {}).get("is_admin", False)

        if log_header:
            with open(user_log, "a") as f:
                f.write(log_header)
                f.flush()

        info = bot_processes.setdefault(user_id, {})
        info["status"] = {"running": True, "message": "Refreshing tokens..."}

        process = None
        try:
            env = _bot_env_for_user(user_id, is_admin)
            skip_refresh = _tokens_recently_validated(user_id)
            if skip_refresh:
                append_user_log(user_id, "Tokens recently validated — skipping pre-run refresh.")
                token_ok = {a: True for a in (["A1", "A2"] if is_admin else ["A1"])}
            else:
                append_user_log(user_id, "Checking Codementor token validity...")
                token_ok = _quick_token_check(user_id, is_admin)
                append_user_log(
                    user_id,
                    "Token status: "
                    + ", ".join(
                        f"{k}={'valid' if v else 'needs refresh'}" for k, v in token_ok.items()
                    ),
                )
                if all(token_ok.values()):
                    _mark_tokens_validated(user_id)

            to_refresh = [a for a, ok in token_ok.items() if not ok]
            user_tokens = load_json_file(USER_DATA_DIR / f"{user_id}_tokens.json", {})

            if to_refresh:
                info["status"] = {"running": True, "message": "Refreshing tokens..."}
                if (
                    "A1" in to_refresh
                    and not (user_tokens.get("A1") or {}).get("refresh_token")
                ):
                    append_user_log(
                        user_id,
                        "A1 has no REFRESH_TOKEN — headless browser cannot log in. "
                        "Open Tokens page, paste full document.cookie from Codementor, then re-run.",
                    )
                    to_refresh = [a for a in to_refresh if a != "A1"]

                if to_refresh:
                    env["REFRESH_ACCOUNTS"] = ",".join(to_refresh)
                    append_user_log(
                        user_id,
                        f"Auto-refreshing: {', '.join(to_refresh)} (live output below)...",
                    )
                    _stream_subprocess_to_log(
                        user_id,
                        [".venv/bin/python", "-u", "scripts/refresh_auth.py"],
                        env,
                        timeout_sec=60,
                        label="token refresh",
                    )
                elif "A1" in token_ok and not token_ok["A1"]:
                    append_user_log(user_id, "Skipping A1 browser refresh — add REFRESH_TOKEN via Tokens page.")
            else:
                append_user_log(user_id, "Tokens valid — skipping browser refresh.")

            info["status"] = {"running": True, "message": "Running bot..."}
            append_user_log(user_id, "Starting bot subprocess...")
            process = subprocess.Popen(
                [".venv/bin/python", "-u", "codementor_bot_hybrid.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            info["process"] = process
            info["running"] = True

            with open(user_log, "a") as f:
                start = time.time()
                for line in iter(process.stdout.readline, ""):
                    if line:
                        f.write(line)
                        f.flush()
                    if time.time() - start > 600:
                        process.terminate()
                        break

            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            ok = process.returncode == 0
            info["status"] = {
                "running": False,
                "last_run": datetime.now().isoformat(),
                "message": "Completed successfully" if ok else f"Error: {process.returncode}",
            }
            return ok, "ok"
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            info["status"] = {"running": False, "message": "Timeout - bot took too long"}
            return False, "error"
        except Exception as e:
            info["status"] = {"running": False, "message": f"Error: {str(e)}"}
            return False, "error"
        finally:
            info["running"] = False
            st = info.get("status") or {}
            st["running"] = False
            info["status"] = st

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'landing'
login_manager.session_protection = 'basic'


def _safe_redirect_target(target: str | None) -> str | None:
    if not target:
        return None
    if target.startswith('/') and not target.startswith('//'):
        return target
    return None


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Login required'}), 401
    return redirect(url_for('landing', next=request.path))

class User(UserMixin):
    def __init__(self, user_id, email, a1_email=None, a1_password=None):
        self.id = user_id
        self.email = email
        self.a1_email = a1_email
        self.a1_password = a1_password
    
    def get_user_data(self):
        """Get user's private data file path"""
        return USER_DATA_DIR / f"{self.id}_data.json"
    
    def get_requests_db(self):
        """Get user's requests database"""
        return USER_DATA_DIR / f"{self.id}_requests.json"
    
    def get_tokens_file(self):
        """Get user's tokens file"""
        return USER_DATA_DIR / f"{self.id}_tokens.json"

@login_manager.user_loader
def load_user(user_id):
    users = load_json_file(USERS_FILE, {})
    user_data = users.get(user_id)
    if user_data:
        return User(
            user_id=user_id,
            email=user_data.get('email'),
            a1_email=user_data.get('a1_email'),
            a1_password=user_data.get('a1_password')
        )
    return None

def get_global_config():
    """Get global config (A2 is shared)"""
    return load_json_file(CONFIG_FILE, {})

def get_user_config(user_id):
    """Get user-specific config merged with global"""
    global_config = get_global_config()
    creds = get_user_credentials(user_id)

    config = {
        'a2_email': global_config.get('account_a2', {}).get('email'),
        'a2_password': global_config.get('account_a2', {}).get('password'),
        'a1_email': creds.get('a1_email'),
        'a1_password': creds.get('a1_password'),
        'message': creds.get('message') or global_config.get('message', 'I am interested in your request'),
        'check_interval': global_config.get('check_interval_minutes', 5),
    }
    return config

# ========== AUTHENTICATION ROUTES ==========

@app.route('/')
def landing():
    """Landing page with login/register"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration - requires admin password"""
    # Check for admin session or admin password
    is_admin_session = session.get('is_admin', False)
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        admin_password = request.form.get('admin_password', '')
        
        # Verify admin access
        if not is_admin_session:
            if not admin_password:
                flash('Admin password required to create accounts', 'error')
                return redirect(url_for('register'))
            if not check_password_hash(get_admin_password(), admin_password):
                flash('Invalid admin password', 'error')
                return redirect(url_for('register'))
        
        if not email or not password:
            flash('Email and password are required', 'error')
            return redirect(url_for('register'))
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return redirect(url_for('register'))
        
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
            return redirect(url_for('register'))
        
        users = load_json_file(USERS_FILE, {})
        
        # Check if email exists
        for user_id, user_data in users.items():
            if user_data.get('email', '').lower() == email:
                flash('Email already registered', 'error')
                return redirect(url_for('register'))
        
        # Create new user
        user_id = secrets.token_hex(8)
        users[user_id] = {
            'email': email,
            'password_hash': generate_password_hash(password),
            'created_at': datetime.now().isoformat(),
            'a1_email': None,
            'a1_password': None,
            'message': None,
            'onboarding_complete': False
        }
        save_json_file(USERS_FILE, users)
        
        # Initialize user data files
        user_data_file = USER_DATA_DIR / f"{user_id}_data.json"
        save_json_file(user_data_file, {
            'user_id': user_id,
            'email': email,
            'a1_email': None,
            'a1_password': None,
            'message': None,
            'created_at': datetime.now().isoformat()
        })
        
        # Initialize empty requests DB for user
        user_requests_file = USER_DATA_DIR / f"{user_id}_requests.json"
        save_json_file(user_requests_file, {})
        
        # Initialize empty tokens for user
        user_tokens_file = USER_DATA_DIR / f"{user_id}_tokens.json"
        save_json_file(user_tokens_file, {})
        
        flash('Account created successfully!', 'success')
        
        # If admin is creating the account, stay on register page for more accounts
        if is_admin_session:
            return redirect(url_for('register'))
        else:
            # Regular flow - redirect to login
            return redirect(url_for('login'))
    
    return render_template('register.html', is_admin=is_admin_session)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        users = load_json_file(USERS_FILE, {})
        
        # Find user by email
        user_id = None
        user_data = None
        for uid, data in users.items():
            if data.get('email', '').lower() == email:
                user_id = uid
                user_data = data
                break
        
        if user_data and check_password_hash(user_data.get('password_hash', ''), password):
            user = User(user_id, email, user_data.get('a1_email'), user_data.get('a1_password'))
            session.permanent = True
            login_user(user, remember=True)

            if user_data.get('is_admin'):
                session['is_admin'] = True

            dest = _safe_redirect_target(
                request.form.get('next') or request.args.get('next')
            )

            if not user_data.get('onboarding_complete'):
                flash('Please complete onboarding to continue', 'info')
                return redirect(url_for('onboarding'))

            flash('Login successful!', 'success')
            return redirect(dest or url_for('dashboard'))
        else:
            flash('Invalid email or password', 'error')
            return redirect(url_for('login'))
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """User logout"""
    logout_user()
    flash('Logged out successfully', 'info')
    return redirect(url_for('landing'))

# ========== ONBOARDING ==========

@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    """Onboarding - collect A1 credentials"""
    users = load_json_file(USERS_FILE, {})
    user_data = users.get(current_user.id, {})
    
    if request.method == 'POST':
        a1_email = request.form.get('a1_email', '').strip()
        a1_password = request.form.get('a1_password', '')
        message = request.form.get('message', '').strip()
        
        if not a1_email or not a1_password:
            flash('A1 email and password are required', 'error')
            return redirect(url_for('onboarding'))
        
        # Update user data
        users[current_user.id]['a1_email'] = a1_email
        users[current_user.id]['a1_password'] = a1_password
        users[current_user.id]['message'] = message
        users[current_user.id]['onboarding_complete'] = True
        save_json_file(USERS_FILE, users)
        
        # Update user data file
        user_data_file = USER_DATA_DIR / f"{current_user.id}_data.json"
        user_data = load_json_file(user_data_file, {})
        user_data.update({
            'a1_email': a1_email,
            'a1_password': a1_password,
            'message': message,
            'onboarding_complete': True
        })
        save_json_file(user_data_file, user_data)
        
        # Auto-extract A1 token from Codementor credentials (background)
        uid = current_user.id
        is_admin = users[uid].get('is_admin', False)
        threading.Thread(
            target=lambda: _run_auth_refresh_for_user(uid, is_admin),
            daemon=True,
        ).start()
        
        flash('Onboarding complete! Fetching Codementor session automatically…', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('onboarding.html', 
                          user_data=user_data,
                          global_config=get_global_config())

# ========== DASHBOARD & MAIN ROUTES ==========

@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard"""
    user_config = get_user_config(current_user.id)
    user_requests = load_json_file(current_user.get_requests_db(), {})
    user_tokens = load_merged_tokens(current_user.get_tokens_file())
    
    token_status = {}
    for acc in ['A1', 'A2']:
        token = user_tokens.get(acc, {}).get('access_token', '')
        token_status[acc] = {
            'has_token': bool(token),
            'token_preview': token[:20] + '...' if len(token) > 20 else token,
            'email': user_tokens.get(acc, {}).get('user_email', 
                     user_config.get(f'{acc.lower()}_email', 'Not set'))
        }
    
    # Check if bot is running for this user
    user_bot_status = get_bot_status_for_user(current_user.id)
    
    return render_template('dashboard.html',
                         token_status=token_status,
                         bot_status=user_bot_status,
                         total_requests=len(user_requests),
                         config=user_config,
                         user=current_user)

@app.route('/tokens')
@login_required
def tokens_page():
    """Token management page"""
    user_tokens = load_merged_tokens(current_user.get_tokens_file())
    return render_template('tokens.html', tokens=user_tokens)

@app.route('/requests')
@login_required
def requests_page():
    """User's requests viewer page (data loaded via /api/requests)."""
    return render_template('requests.html')

@app.route('/logs')
@login_required
def logs_page():
    """View logs page"""
    user_log = USER_DATA_DIR / f"{current_user.id}.log"
    log_content = ""
    if user_log.exists():
        with open(user_log) as f:
            log_content = f.read()
    return render_template('logs.html', logs=log_content)

@app.route('/config', methods=['GET', 'POST'])
@login_required
def config_page():
    """Configuration page"""
    if request.method == 'POST':
        # Handle form submission
        user_data_file = current_user.get_user_data()
        user_data = load_json_file(user_data_file, {})
        
        # Update user data with form values
        user_data['a1_email'] = request.form.get('a1_email', '').strip()
        user_data['a1_password'] = request.form.get('a1_password', '')
        user_data['message'] = request.form.get('message', '').strip()
        save_json_file(user_data_file, user_data)
        
        # Also update in users.json
        users = load_json_file(USERS_FILE, {})
        if current_user.id in users:
            users[current_user.id].update({
                'a1_email': user_data.get('a1_email'),
                'a1_password': user_data.get('a1_password'),
                'message': user_data.get('message')
            })
            save_json_file(USERS_FILE, users)
        
        flash('Configuration saved successfully!', 'success')
        return redirect(url_for('config_page'))
    
    user_data = load_json_file(current_user.get_user_data(), {})
    global_config = get_global_config()
    return render_template('config.html', 
                          user_data=user_data,
                          global_config=global_config)

@app.route('/uclients')
@login_required
def uclients_page():
    """User clients page - shows Codementor contacts"""
    return render_template('uclients.html')


@app.route('/messages')
@app.route('/umessages')
@login_required
def umessages_page():
    """Inbox — all Codementor threads with unread / read grouping."""
    return render_template('umessages.html')

# ========== API ROUTES ==========

@app.route('/api/user/config', methods=['GET'])
@login_required
def get_user_config_api():
    """Get user configuration"""
    config = get_user_config(current_user.id)
    return jsonify(config)

@app.route('/api/user/config', methods=['POST'])
@login_required
def update_user_config():
    """Update user configuration"""
    data = request.json
    
    # Load and update user data
    user_data_file = current_user.get_user_data()
    user_data = load_json_file(user_data_file, {})
    
    if 'a1_email' in data:
        user_data['a1_email'] = data['a1_email']
    if 'a1_password' in data:
        user_data['a1_password'] = data['a1_password']
    if 'message' in data:
        user_data['message'] = data['message']
    
    save_json_file(user_data_file, user_data)
    
    # Also update in users.json
    users = load_json_file(USERS_FILE, {})
    if current_user.id in users:
        users[current_user.id].update({
            'a1_email': user_data.get('a1_email'),
            'a1_password': user_data.get('a1_password'),
            'message': user_data.get('message')
        })
        save_json_file(USERS_FILE, users)
    
    return jsonify({"success": True, "message": "Configuration saved"})

@app.route('/api/tokens', methods=['GET'])
@login_required
def get_tokens():
    """Get current tokens"""
    user_tokens = load_merged_tokens(current_user.get_tokens_file())
    # Mask full tokens for security
    safe_tokens = {}
    for acc, data in user_tokens.items():
        safe_tokens[acc] = {
            'has_token': bool(data.get('access_token')),
            'preview': data.get('access_token', '')[:20] + '...' if data.get('access_token') else '',
            'email': data.get('user_email', '')
        }
    return jsonify(safe_tokens)

@app.route('/api/tokens', methods=['POST'])
@login_required
def update_tokens():
    """Update tokens"""
    data = request.json
    user_tokens_file = current_user.get_tokens_file()
    current = load_json_file(user_tokens_file, {})

    if 'A1' in data:
        if 'A1' not in current:
            current['A1'] = {}
        a1_raw = data['A1'].get('access_token', '')
        current['A1']['access_token'] = extract_token_from_cookie_string(a1_raw) or a1_raw
        refresh = data['A1'].get('refresh_token') or extract_refresh_token(a1_raw)
        if refresh:
            current['A1']['refresh_token'] = refresh
        current['A1']['user_email'] = data['A1'].get('user_email', '')

    if 'A2' in data:
        global_tok = load_json_file(TOKENS_FILE, {})
        if not isinstance(global_tok, dict):
            global_tok = {}
        if 'A2' not in global_tok:
            global_tok['A2'] = {}
        global_tok['A2']['access_token'] = data['A2'].get('access_token', '')
        if data['A2'].get('refresh_token'):
            global_tok['A2']['refresh_token'] = data['A2'].get('refresh_token')
        global_tok['A2']['user_email'] = data['A2'].get('user_email', '')
        save_json_file(TOKENS_FILE, global_tok)

    # A2 is stored globally; drop any stale per-user A2 copy
    current.pop('A2', None)
    save_json_file(user_tokens_file, current)
    return jsonify({"success": True, "message": "Tokens updated"})

def _requests_list_payload(user) -> dict:
    """Build JSON list for the requests UI."""
    data = load_json_file(user.get_requests_db(), {})
    items = []
    for rid, req in data.items():
        if not isinstance(req, dict):
            continue
        items.append({
            "request_id": rid,
            "random_key": req.get("random_key", rid),
            "title": req.get("title", "Untitled"),
            "author": req.get("author", ""),
            "budget": req.get("budget", ""),
            "request_type": req.get("request_type", ""),
            "tags": req.get("tags", []) or [],
            "interested_count": req.get("interested_count", 0),
            "processed_at": req.get("processed_at", ""),
            "url": req.get("url", ""),
            "description": (req.get("description") or "")[:200],
        })
    items.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"success": True, "requests": items, "total": len(items)}


@app.route('/api/requests', methods=['GET'])
@login_required
def get_requests():
    """List stored requests for the requests page (filters/pagination)."""
    return jsonify(_requests_list_payload(current_user))

@app.route('/api/requests/clear', methods=['POST'])
@login_required
def clear_requests():
    """Clear user's requests database"""
    save_json_file(current_user.get_requests_db(), {})
    return jsonify({"success": True, "message": "Requests database cleared"})

def _run_auth_refresh_for_user(user_id: str, is_admin: bool = False, accounts=None):
    """Refresh tokens using stored credentials (automated, no manual paste)."""
    create_user_config_file(user_id)
    old_env = {}
    keys = ('USER_ID', 'IS_ADMIN', 'USER_TOKENS_FILE', 'USER_REQUESTS_FILE', 'USER_CONFIG_FILE')
    for key in keys:
        old_env[key] = os.environ.get(key)
    os.environ['USER_ID'] = user_id
    os.environ['IS_ADMIN'] = 'true' if is_admin else 'false'
    os.environ['USER_TOKENS_FILE'] = str(USER_DATA_DIR / f"{user_id}_tokens.json")
    os.environ['USER_REQUESTS_FILE'] = str(USER_DATA_DIR / f"{user_id}_requests.json")
    os.environ['USER_CONFIG_FILE'] = str(USER_DATA_DIR / f"{user_id}_bot_config.json")
    try:
        service = TokenAuthService()
        targets = list(accounts or ['A1'])
        if is_admin and 'A2' not in targets:
            targets.append('A2')
        return asyncio.run(service.refresh_accounts(targets))
    finally:
        for key, val in old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@app.route('/api/auth/refresh', methods=['POST'])
@login_required
def api_refresh_auth():
    """Automatically refresh Codementor tokens for the current user."""
    users = load_json_file(USERS_FILE, {})
    is_admin = users.get(current_user.id, {}).get('is_admin', False)
    try:
        results = _run_auth_refresh_for_user(current_user.id, is_admin)
        return jsonify({
            "success": all(results.values()),
            "results": results,
            "message": "Tokens refreshed" if all(results.values()) else "Some accounts failed — check credentials",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/clients', methods=['GET'])
@login_required
def get_clients():
    """Fetch all Codementor chat contacts (paginated) using A1 token(s)."""
    from cmbot.api.contacts import fetch_all_contacts

    try:
        users = load_json_file(USERS_FILE, {})
        is_admin = users.get(current_user.id, {}).get('is_admin', False)
        session_is_admin = session.get('is_admin', False)
        force = request.args.get('force', '').lower() in ('1', 'true', 'yes')
        online_only = request.args.get('online', '').lower() in ('1', 'true', 'yes')
        offline_only = request.args.get('offline', '').lower() in ('1', 'true', 'yes')

        user_data_file = current_user.get_user_data()
        user_data = load_json_file(user_data_file, {})

        if not force and user_data.get('cached_clients'):
            cached_at = user_data.get('cached_clients_time')
            if cached_at:
                try:
                    last = datetime.fromisoformat(str(cached_at).replace('Z', ''))
                    if datetime.now() - last < timedelta(minutes=30):
                        clients = list(user_data['cached_clients'])
                        favorites = user_data.get('client_favorites', {})
                        for client in clients:
                            client['favorited'] = favorites.get(client.get('username'), False)
                            uname = client.get('username') or ''
                            client['chat_url'] = f'https://www.codementor.io/messages/{uname}' if uname else ''
                        if online_only:
                            clients = [c for c in clients if c.get('online_status') == 'online']
                        elif offline_only:
                            clients = [c for c in clients if c.get('online_status') != 'online']
                        return jsonify({
                            'success': True,
                            'clients': clients,
                            'cached': True,
                            'cached_at': cached_at,
                            'total': len(clients),
                        })
                except (ValueError, TypeError):
                    pass

        tokens_to_query = []

        if is_admin or session_is_admin:
            for user_id, user_info in users.items():
                user_tokens_file = USER_DATA_DIR / f"{user_id}_tokens.json"
                if user_tokens_file.exists():
                    user_tokens = load_merged_tokens(user_tokens_file)
                    a1_token = _a1_access_from_storage(user_tokens)
                    if a1_token:
                        tokens_to_query.append({
                            'token': a1_token,
                            'account': user_info.get('email', user_id),
                            'user_id': user_id,
                        })

        if not tokens_to_query:
            user_tokens = load_merged_tokens(current_user.get_tokens_file())
            a1_token = _a1_access_from_storage(user_tokens)
            if a1_token:
                tokens_to_query.append({
                    'token': a1_token,
                    'account': current_user.email,
                    'user_id': current_user.id,
                })

        if not tokens_to_query:
            return jsonify({
                'success': False,
                'error': 'A1 token not configured. Please go to Tokens page and add your ACCESS_TOKEN from Codementor cookies.',
            })

        async def fetch_for_account(token_info):
            try:
                clients, pages = await fetch_all_contacts(token_info['token'])
                for client in clients:
                    client['_source_account'] = token_info['account']
                    client['_source_user_id'] = token_info['user_id']
                return {
                    'success': True,
                    'clients': clients,
                    'account': token_info['account'],
                    'pages': pages,
                }
            except PermissionError:
                user_data_file = USER_DATA_DIR / f"{token_info['user_id']}_data.json"
                cached = load_json_file(user_data_file, {}).get('cached_clients')
                if cached:
                    return {
                        'success': True,
                        'clients': cached,
                        'account': token_info['account'],
                        'cached': True,
                        'error': 'Token expired, using cached data',
                    }
                return {'success': False, 'status': 401, 'account': token_info['account'], 'error': 'Token expired'}
            except Exception as e:
                return {'success': False, 'error': str(e), 'account': token_info['account']}
        
        # Fetch from all accounts concurrently
        async def fetch_all():
            tasks = [fetch_for_account(ti) for ti in tokens_to_query]
            return await asyncio.gather(*tasks)
        
        results = asyncio.run(fetch_all())
        
        # Aggregate all clients
        all_clients = []
        failed_accounts = []
        accounts_with_cached_data = []
        
        for result in results:
            if result.get('success'):
                all_clients.extend(result['clients'])
                # Track which accounts used cached data
                if result.get('cached'):
                    accounts_with_cached_data.append(result.get('account', 'Unknown'))
            else:
                failed_accounts.append(result.get('account', 'Unknown'))
        
        # Deduplicate by username (keep most recent)
        seen = {}
        for client in all_clients:
            username = client.get('username')
            if username:
                if username not in seen or (client.get('last_message_at', 0) > seen[username].get('last_message_at', 0)):
                    seen[username] = client
        
        clients = list(seen.values())
        
        # Get current user's favorites
        user_data_file = current_user.get_user_data()
        user_data = load_json_file(user_data_file, {})
        favorites = user_data.get('client_favorites', {})
        
        # Mark favorited + chat links
        for client in clients:
            client["favorited"] = favorites.get(client.get("username"), False)
            uname = client.get("username") or ""
            client["chat_url"] = f"https://www.codementor.io/messages/{uname}" if uname else ""
        
        # Store aggregated clients in admin's cache
        user_data['cached_clients'] = clients
        user_data['cached_clients_time'] = datetime.now().isoformat()
        save_json_file(user_data_file, user_data)
        
        if online_only:
            clients = [c for c in clients if c.get('online_status') == 'online']
        elif offline_only:
            clients = [c for c in clients if c.get('online_status') != 'online']

        response_data = {
            "success": True,
            "clients": clients,
            "total": len(clients),
            "total_accounts": len(tokens_to_query),
            "failed_accounts": failed_accounts if failed_accounts else None,
            "accounts_with_cached_data": accounts_with_cached_data if accounts_with_cached_data else None,
        }
        
        if (is_admin or session_is_admin) and len(tokens_to_query) > 1:
            response_data['is_admin_view'] = True
            response_data['accounts_scanned'] = [t['account'] for t in tokens_to_query]
        
        return jsonify(response_data)
            
    except Exception as e:
        # Try to return cached data if available
        user_data = load_json_file(current_user.get_user_data(), {})
        if 'cached_clients' in user_data:
            clients = user_data['cached_clients']
            favorites = user_data.get('client_favorites', {})
            
            for client in clients:
                client["favorited"] = favorites.get(client.get("username"), False)
                uname = client.get("username") or ""
                client["chat_url"] = f"https://www.codementor.io/messages/{uname}" if uname else ""

            return jsonify({
                "success": True, 
                "clients": clients,
                "cached": True,
                "cached_at": user_data.get('cached_clients_time', 'unknown')
            })
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/messages', methods=['GET'])
@login_required
def get_messages_inbox():
    """All message threads with preview; grouped unread vs read in UI."""
    from cmbot.api.inbox import fetch_inbox

    force = request.args.get('force', '').lower() in ('1', 'true', 'yes')
    user_data_file = current_user.get_user_data()
    user_data = load_json_file(user_data_file, {})

    if not force and user_data.get('cached_inbox'):
        cached_at = user_data.get('cached_inbox_time')
        if cached_at:
            try:
                last = datetime.fromisoformat(str(cached_at).replace('Z', ''))
                if datetime.now() - last < timedelta(minutes=20):
                    inbox = user_data['cached_inbox']
                    unread = [m for m in inbox if m.get('unread')]
                    read = [m for m in inbox if not m.get('unread')]
                    return jsonify({
                        'success': True,
                        'messages': inbox,
                        'unread': unread,
                        'read': read,
                        'total': len(inbox),
                        'unread_count': len(unread),
                        'cached': True,
                        'cached_at': cached_at,
                    })
            except (ValueError, TypeError):
                pass

    token, err = get_a1_access_token(current_user.id, refresh_if_needed=False)
    if not token:
        token, err = get_valid_a1_token(current_user.id)
    if not token:
        if user_data.get('cached_inbox'):
            inbox = user_data['cached_inbox']
            unread = [m for m in inbox if m.get('unread')]
            return jsonify({
                'success': True,
                'messages': inbox,
                'unread': unread,
                'read': [m for m in inbox if not m.get('unread')],
                'total': len(inbox),
                'unread_count': len(unread),
                'cached': True,
                'warning': err,
            })
        return jsonify({'success': False, 'error': err})

    try:
        inbox, pages = asyncio.run(
            asyncio.wait_for(fetch_inbox(token, max_enrich=300), timeout=180)
        )
        user_data['cached_inbox'] = inbox
        user_data['cached_inbox_time'] = datetime.now().isoformat()
        save_json_file(user_data_file, user_data)
        unread = [m for m in inbox if m.get('unread')]
        read = [m for m in inbox if not m.get('unread')]
        return jsonify({
            'success': True,
            'messages': inbox,
            'unread': unread,
            'read': read,
            'total': len(inbox),
            'unread_count': len(unread),
            'pages': pages,
        })
    except asyncio.TimeoutError:
        return jsonify({'success': False, 'error': 'Inbox load timed out — try again'})
    except PermissionError:
        return jsonify({'success': False, 'error': 'A1 token expired — update on Tokens page'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/messages/unread-count', methods=['GET'])
@login_required
def messages_unread_count():
    """Lightweight unread badge from cached inbox."""
    user_data = load_json_file(current_user.get_user_data(), {})
    inbox = user_data.get('cached_inbox') or []
    count = sum(1 for m in inbox if m.get('unread'))
    return jsonify({'success': True, 'unread_count': count})


@app.route('/api/messages/mark-all-read', methods=['POST'])
@login_required
def mark_all_messages_read():
    """Mark all unread threads read on Codementor."""
    from cmbot.api.inbox import mark_all_read

    token, err = get_a1_access_token(current_user.id, refresh_if_needed=False)
    if not token:
        token, err = get_valid_a1_token(current_user.id)
    if not token:
        return jsonify({'success': False, 'error': err})

    user_data = load_json_file(current_user.get_user_data(), {})
    inbox = user_data.get('cached_inbox') or []
    unread_users = [m['username'] for m in inbox if m.get('unread') and m.get('username')]
    if not unread_users:
        return jsonify({'success': True, 'message': 'No unread messages', 'marked': 0})

    try:
        marked = asyncio.run(mark_all_read(token, unread_users))
        for m in inbox:
            if m.get('username') in unread_users:
                m['unread'] = False
        user_data['cached_inbox'] = inbox
        user_data['cached_inbox_time'] = datetime.now().isoformat()
        save_json_file(current_user.get_user_data(), user_data)
        return jsonify({'success': True, 'message': f'Marked {marked} thread(s) read', 'marked': marked})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/messages/<username>/thread', methods=['GET'])
@login_required
def get_message_thread(username):
    """Full conversation history with one user."""
    from cmbot.api.inbox import fetch_thread, mark_thread_read

    username = (username or '').strip().lstrip('@')
    if not username:
        return jsonify({'success': False, 'error': 'Username required'})

    token, err = get_a1_access_token(current_user.id, refresh_if_needed=False)
    if not token:
        token, err = get_valid_a1_token(current_user.id)
    if not token:
        return jsonify({'success': False, 'error': err})

    try:
        thread, err = asyncio.run(fetch_thread(token, username))
        if err:
            return jsonify({'success': False, 'error': err})
        if not thread:
            return jsonify({'success': False, 'error': 'Thread not found'})
        asyncio.run(mark_thread_read(token, username))
        user_data = load_json_file(current_user.get_user_data(), {})
        for m in user_data.get('cached_inbox') or []:
            if m.get('username') == username:
                m['unread'] = False
        save_json_file(current_user.get_user_data(), user_data)
        return jsonify({'success': True, 'thread': thread})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/messages/<username>/read', methods=['POST'])
@login_required
def mark_message_read(username):
    """Mark one thread read."""
    from cmbot.api.inbox import mark_thread_read

    token, err = get_a1_access_token(current_user.id, refresh_if_needed=False)
    if not token:
        return jsonify({'success': False, 'error': err or 'Token missing'})
    username = (username or '').strip().lstrip('@')
    try:
        ok = asyncio.run(mark_thread_read(token, username))
        if ok:
            user_data = load_json_file(current_user.get_user_data(), {})
            for m in user_data.get('cached_inbox') or []:
                if m.get('username') == username:
                    m['unread'] = False
            save_json_file(current_user.get_user_data(), user_data)
        return jsonify({'success': ok, 'message': 'Marked read' if ok else 'Failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/clients/message', methods=['POST'])
@login_required
def send_client_message():
    """Send a direct message to a client by username."""
    from cmbot.messaging import send_chat_message

    data = request.json or {}
    username = (data.get("username") or "").strip().lstrip("@")
    content = (data.get("message") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username required"})

    user_config = get_user_config(current_user.id)
    if not content:
        content = user_config.get("message", "Hello!")

    token, err = get_a1_access_token(current_user.id, refresh_if_needed=False)
    if not token:
        return jsonify({"success": False, "error": err})

    async def _send(access: str):
        return await send_chat_message(access, username, content)

    try:
        ok, msg = asyncio.run(_send(token))
        if not ok and any(x in (msg or '').lower() for x in ('401', 'expired', 'invalid', 'unauthorized')):
            token, err = get_valid_a1_token(current_user.id)
            if not token:
                return jsonify({"success": False, "error": err or msg})
            ok, msg = asyncio.run(_send(token))
        if ok:
            return jsonify({
                "success": True,
                "message": msg,
                "chat_url": f"https://www.codementor.io/messages/{username}",
            })
        return jsonify({"success": False, "error": msg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/clients/favorite', methods=['POST'])
@login_required
def toggle_client_favorite():
    """Toggle favorite status for a client (stored locally)"""
    try:
        data = request.json
        username = data.get('username')
        favorited = data.get('favorited', False)
        
        if not username:
            return jsonify({"success": False, "error": "Username required"})
        
        # Store in user data
        user_data_file = current_user.get_user_data()
        user_data = load_json_file(user_data_file, {})
        
        if 'client_favorites' not in user_data:
            user_data['client_favorites'] = {}
        
        if favorited:
            user_data['client_favorites'][username] = True
        else:
            user_data['client_favorites'].pop(username, None)
        
        save_json_file(user_data_file, user_data)
        
        return jsonify({"success": True, "favorited": favorited})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ========== BOT CONTROL ==========

def create_user_config_file(user_id):
    """Create bot config and keep user_data JSON in sync with users.json."""
    config = get_user_config(user_id)
    user_config_file = USER_DATA_DIR / f"{user_id}_bot_config.json"
    save_json_file(user_config_file, config)

    creds = get_user_credentials(user_id)
    data_file = USER_DATA_DIR / f"{user_id}_data.json"
    data = load_json_file(data_file, {})
    data.update({
        "user_id": user_id,
        "a1_email": creds.get("a1_email"),
        "a1_password": creds.get("a1_password"),
        "message": creds.get("message"),
        "onboarding_complete": creds.get("onboarding_complete"),
    })
    save_json_file(data_file, data)
    return user_config_file

@app.route('/api/bot/run', methods=['POST'])
@login_required
def run_bot():
    """Run the bot for current user"""
    user_id = current_user.id
    
    if is_bot_process_alive(user_id):
        return jsonify({"success": False, "message": "Bot is already running for your account"})

    users = load_json_file(USERS_FILE, {})
    if not users.get(user_id, {}).get('onboarding_complete'):
        return jsonify({"success": False, "message": "Please complete onboarding first"})

    log_header = (
        f"\n{'='*60}\n"
        f"Run at {datetime.now()} for user {current_user.email}\n"
        f"{'='*60}\n"
    )

    def run_bot_thread():
        ok, reason = execute_bot_cycle(user_id, log_header=log_header)
        if reason == "busy":
            user_log = USER_DATA_DIR / f"{user_id}.log"
            with open(user_log, "a") as f:
                f.write(f"{datetime.now()} - Bot run skipped (another run in progress)\n")

    threading.Thread(target=run_bot_thread, daemon=True).start()
    return jsonify({"success": True, "message": "Bot started - check logs for progress"})

@app.route('/api/bot/status', methods=['GET'])
@login_required
def get_bot_status():
    """Get bot status for current user"""
    user_id = current_user.id
    return jsonify(get_bot_status_for_user(user_id))

@app.route('/api/bot/stop', methods=['POST'])
@login_required
def stop_bot():
    """Stop the running bot for current user"""
    user_id = current_user.id
    
    reconcile_bot_process(user_id)
    if not is_bot_process_alive(user_id):
        return jsonify({"success": False, "message": "Bot is not running"})

    try:
        process = bot_processes.get(user_id, {}).get('process')
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        
        bot_processes[user_id]['running'] = False
        bot_processes[user_id]['status'] = {
            'running': False,
            'message': 'Stopped by user'
        }
        
        return jsonify({"success": True, "message": "Bot stopped"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ========== BACKGROUND SCHEDULER ==========

class BackgroundScheduler:
    """Manages automatic background A1 scanning and processing"""
    
    def __init__(self, user_id, check_interval_minutes=5):
        self.user_id = user_id
        self.check_interval = check_interval_minutes * 60  # Convert to seconds
        self.running = False
        self.thread = None
        self.last_run = None
        self.next_run = None
        self.run_count = 0
        self.user_log = USER_DATA_DIR / f"{user_id}.log"
    
    def start(self):
        """Start the background scheduler"""
        if self.running:
            return False
        
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._log("[Background Scheduler] Started")
        return True
    
    def stop(self):
        """Stop the background scheduler"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self._log("[Background Scheduler] Stopped")
        return True
    
    def _log(self, message):
        """Write to user's log file"""
        with open(self.user_log, 'a') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
            f.flush()
    
    def _run_loop(self):
        """Main scheduler loop"""
        while self.running:
            try:
                # Check if user has valid tokens and config
                users = load_json_file(USERS_FILE, {})
                if self.user_id not in users:
                    self._log("[Background Scheduler] User not found, stopping")
                    break
                
                user_data = users[self.user_id]
                if not user_data.get('onboarding_complete'):
                    self._log("[Background Scheduler] Onboarding incomplete, skipping")
                    time.sleep(self.check_interval)
                    continue
                
                # Check if A1 credentials exist
                if not user_data.get('a1_email') or not user_data.get('a1_password'):
                    self._log("[Background Scheduler] A1 credentials missing, skipping")
                    time.sleep(self.check_interval)
                    continue
                
                self.next_run = datetime.now() + timedelta(seconds=self.check_interval)
                self._log(f"[Background Scheduler] Starting run #{self.run_count + 1}")
                
                # Run the bot
                success = self._run_bot()
                
                if success:
                    self.run_count += 1
                    self.last_run = datetime.now()
                    self._log(f"[Background Scheduler] Run #{self.run_count} completed")
                else:
                    self._log("[Background Scheduler] Run failed")
                
                # Wait for next interval
                self._log(f"[Background Scheduler] Next run in {self.check_interval // 60} minutes")
                time.sleep(self.check_interval)
                
            except Exception as e:
                self._log(f"[Background Scheduler] Error: {e}")
                time.sleep(60)  # Wait 1 minute on error before retry
    
    def _run_bot(self):
        """Execute the bot in background (serialized with manual runs)."""
        log_header = (
            f"\n{'='*60}\n"
            f"[Background Run] {datetime.now()}\n"
            f"{'='*60}\n"
        )
        ok, reason = execute_bot_cycle(self.user_id, log_header=log_header)
        if reason == "busy":
            self._log("[Background Scheduler] Bot run in progress, will retry next interval")
        return ok


def restore_all_schedulers() -> None:
    """Re-start schedulers after app/PM2 restart (persisted per user)."""
    users = load_json_file(USERS_FILE, {})
    for user_id, data in users.items():
        if data.get("scheduler_enabled") and data.get("onboarding_complete"):
            _ensure_scheduler_running(user_id)


_schedulers_restored = False


@app.before_request
def _restore_schedulers_once():
    global _schedulers_restored
    if _schedulers_restored:
        return
    _schedulers_restored = True
    try:
        restore_all_schedulers()
    except Exception as e:
        print(f"Scheduler restore warning: {e}")


@app.route('/api/scheduler/start', methods=['POST'])
@login_required
def start_scheduler():
    """Start background scheduler for current user"""
    user_id = current_user.id
    
    # Get check interval from config (default 5 minutes)
    config = get_user_config(user_id)
    interval = config.get('check_interval', 5)
    
    # Stop existing scheduler if running
    if user_id in bot_schedulers and bot_schedulers[user_id].running:
        bot_schedulers[user_id].stop()
    
    # Create and start new scheduler
    scheduler = BackgroundScheduler(user_id, interval)
    success = scheduler.start()
    
    if success:
        bot_schedulers[user_id] = scheduler
        _set_scheduler_persisted(user_id, True, interval)
        return jsonify({
            "success": True,
            "message": f"Background scheduler started (interval: {interval} minutes)",
            "interval_minutes": interval
        })
    else:
        return jsonify({"success": False, "message": "Scheduler already running"})


@app.route('/api/scheduler/stop', methods=['POST'])
@login_required
def stop_scheduler():
    """Stop background scheduler for current user"""
    user_id = current_user.id
    
    if user_id in bot_schedulers and bot_schedulers[user_id].running:
        bot_schedulers[user_id].stop()
        del bot_schedulers[user_id]
    _set_scheduler_persisted(user_id, False)
    return jsonify({"success": True, "message": "Background scheduler stopped"})


@app.route('/api/scheduler/status', methods=['GET'])
@login_required
def get_scheduler_status():
    """Get background scheduler status for current user"""
    user_id = current_user.id
    
    sched = bot_schedulers.get(user_id)
    if not sched or not sched.running:
        sched = _ensure_scheduler_running(user_id)

    if sched and sched.running:
        return jsonify({
            "running": True,
            "persisted": True,
            "run_count": sched.run_count,
            "last_run": sched.last_run.isoformat() if sched.last_run else None,
            "next_run": sched.next_run.isoformat() if sched.next_run else None,
            "interval_minutes": sched.check_interval // 60,
        })

    users = load_json_file(USERS_FILE, {})
    persisted = users.get(user_id, {}).get("scheduler_enabled", False)
    return jsonify({
        "running": False,
        "persisted": persisted,
        "run_count": 0,
        "last_run": None,
        "next_run": None,
    })

# ========== ADMIN ROUTES ==========

ADMIN_PASSWORD_HASH = None

def get_admin_password():
    """Get admin password from config or use default"""
    global ADMIN_PASSWORD_HASH
    if ADMIN_PASSWORD_HASH is None:
        # Default admin password: 'admin123' - change this!
        ADMIN_PASSWORD_HASH = generate_password_hash('admin123')
    return ADMIN_PASSWORD_HASH

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    if request.method == 'POST':
        password = request.form.get('admin_password', '')
        
        # Always check password on POST, regardless of session
        if check_password_hash(get_admin_password(), password):
            session['is_admin'] = True
            flash('Welcome, Admin!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid admin password', 'error')
            # Clear any existing admin session on wrong password
            session.pop('is_admin', None)
            return redirect(url_for('admin_login'))
    
    # GET request - check if already logged in
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    """Admin dashboard - protected"""
    if not session.get('is_admin'):
        flash('Admin access required', 'error')
        return redirect(url_for('admin_login'))
    
    # Load all users
    users_data = load_json_file(USERS_FILE, {})
    global_config = get_global_config()
    
    # Prepare user list with stats
    users = []
    total_requests = 0
    active_bots = 0
    onboarded_count = 0
    
    for user_id, user_data in users_data.items():
        # Count requests for this user
        user_requests = load_json_file(USER_DATA_DIR / f"{user_id}_requests.json", {})
        request_count = len(user_requests)
        total_requests += request_count
        
        # Check if onboarding complete
        onboarded = user_data.get('onboarding_complete', False)
        if onboarded:
            onboarded_count += 1
        
        # Check if user has A1 token
        user_tokens = load_json_file(USER_DATA_DIR / f"{user_id}_tokens.json", {})
        has_a1_token = bool(user_tokens.get('A1', {}).get('access_token'))
        
        users.append({
            'id': user_id,
            'email': user_data.get('email', 'Unknown'),
            'a1_email': user_data.get('a1_email'),
            'has_a1_token': has_a1_token,
            'request_count': request_count,
            'onboarding_complete': onboarded,
            'created_at': user_data.get('created_at', '')
        })
    
    # Count active bots
    for user_id in bot_processes:
        if is_bot_process_alive(user_id):
            active_bots += 1
    
    stats = {
        'total_users': len(users_data),
        'active_bots': active_bots,
        'total_requests': total_requests,
        'onboarded_users': onboarded_count
    }
    
    return render_template('admin_dashboard.html',
                         users=users,
                         stats=stats,
                         global_config=global_config)

@app.route('/admin/logout')
def admin_logout():
    """Admin logout"""
    session.pop('is_admin', None)
    flash('Admin logged out', 'info')
    return redirect(url_for('landing'))

@app.route('/admin/user/<user_id>')
def admin_view_user(user_id):
    """View specific user details"""
    if not session.get('is_admin'):
        flash('Admin access required', 'error')
        return redirect(url_for('admin_login'))
    
    users = load_json_file(USERS_FILE, {})
    user_data = users.get(user_id)
    
    if not user_data:
        flash('User not found', 'error')
        return redirect(url_for('admin_dashboard'))
    
    # Load user's data
    user_requests = load_json_file(USER_DATA_DIR / f"{user_id}_requests.json", {})
    user_tokens = load_merged_tokens(USER_DATA_DIR / f"{user_id}_tokens.json")
    
    return jsonify({
        'user': user_data,
        'requests_count': len(user_requests),
        'requests': list(user_requests.values())[:10],  # Show last 10
        'tokens': {
            'A1': bool(user_tokens.get('A1', {}).get('access_token')),
            'A2': bool(user_tokens.get('A2', {}).get('access_token'))
        }
    })

@app.route('/api/dashboard/bundle', methods=['GET'])
@login_required
def dashboard_bundle():
    """Single poll endpoint for dashboard (bot, scheduler, stats, logs)."""
    user_id = current_user.id
    user_requests = load_json_file(current_user.get_requests_db(), {})
    user_tokens = load_merged_tokens(current_user.get_tokens_file())
    token_status = {}
    for acc in ('A1', 'A2'):
        token = user_tokens.get(acc, {}).get('access_token', '')
        token_status[acc] = {
            'has_token': bool(token),
            'token_preview': token[:20] + '...' if len(token) > 20 else token,
            'email': user_tokens.get(acc, {}).get('user_email', ''),
        }

    sched = bot_schedulers.get(user_id)
    if not sched or not sched.running:
        sched = _ensure_scheduler_running(user_id)
    if sched and sched.running:
        scheduler = {
            'running': True,
            'persisted': True,
            'run_count': sched.run_count,
            'last_run': sched.last_run.isoformat() if sched.last_run else None,
            'next_run': sched.next_run.isoformat() if sched.next_run else None,
            'interval_minutes': sched.check_interval // 60,
        }
    else:
        users = load_json_file(USERS_FILE, {})
        scheduler = {
            'running': False,
            'persisted': users.get(user_id, {}).get('scheduler_enabled', False),
            'run_count': 0,
            'last_run': None,
            'next_run': None,
        }

    bot_status = get_bot_status_for_user(user_id)
    bot_running = bool(bot_status.get('running'))

    user_log = USER_DATA_DIR / f'{user_id}.log'
    log_content = ''
    if user_log.exists():
        with open(user_log, encoding='utf-8', errors='replace') as f:
            content = f.read()
            log_content = content[-12000:] if len(content) > 12000 else content

    return jsonify({
        'bot': bot_status,
        'scheduler': scheduler,
        'stats': {
            'total_requests': len(user_requests),
            'token_status': token_status,
        },
        'logs': log_content,
        'bot_running': bot_running,
    })


@app.route('/api/dashboard/stats', methods=['GET'])
@login_required
def get_dashboard_stats():
    """Get fresh dashboard stats"""
    user_requests = load_json_file(current_user.get_requests_db(), {})
    user_tokens = load_merged_tokens(current_user.get_tokens_file())
    
    token_status = {}
    for acc in ['A1', 'A2']:
        token = user_tokens.get(acc, {}).get('access_token', '')
        token_status[acc] = {
            'has_token': bool(token),
            'token_preview': token[:20] + '...' if len(token) > 20 else token,
            'email': user_tokens.get(acc, {}).get('user_email', '')
        }
    
    return jsonify({
        'total_requests': len(user_requests),
        'token_status': token_status,
        'user_email': current_user.email
    })

@app.route('/api/process/<request_id>', methods=['POST'])
@login_required
def process_single_request(request_id):
    """Process a single request — express interest and message author."""
    from cmbot.messaging import express_interest, send_chat_message

    requests_data = load_json_file(current_user.get_requests_db(), {})
    request_data = requests_data.get(request_id)
    if not request_data:
        return jsonify({"success": False, "error": "Request not found in database"})

    a1_token, err = get_valid_a1_token(current_user.id)
    if not a1_token:
        return jsonify({"success": False, "error": err or "A1 token unavailable"})

    user_config = get_user_config(current_user.id)
    message = user_config.get("message", "I am interested in your request")
    random_key = request_data.get("random_key", request_id)
    author = request_data.get("author", "")

    async def do_process():
        ok, msg = await express_interest(a1_token, random_key, message)
        if not ok:
            return {"success": False, "error": msg}
        if author:
            await send_chat_message(a1_token, author, message)
        request_data["processed_at"] = datetime.now().isoformat()
        requests_data[request_id] = request_data
        save_json_file(current_user.get_requests_db(), requests_data)
        return {"success": True, "message": "Request processed successfully"}

    try:
        return jsonify(asyncio.run(do_process()))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/scan', methods=['POST'])
@login_required
def scan_requests():
    """Trigger a scan for new requests (runs the bot)"""
    # This reuses the existing run_bot logic but returns immediately
    result = run_bot()
    return result


@app.route('/api/logs', methods=['GET'])
@login_required
def get_logs():
    """Tail of user log file for live dashboard updates."""
    user_log = USER_DATA_DIR / f"{current_user.id}.log"
    users = load_json_file(USERS_FILE, {})
    is_admin = users.get(current_user.id, {}).get('is_admin', False)
    bot_running = get_bot_status_for_user(current_user.id).get("running", False)

    log_content = ""
    if user_log.exists():
        with open(user_log, encoding="utf-8", errors="replace") as f:
            content = f.read()
            log_content = content[-12000:] if len(content) > 12000 else content

        if not is_admin:
            filtered_lines = []
            for line in log_content.split("\n"):
                if not line.strip():
                    continue
                line_lower = line.lower()
                if any(skip in line_lower for skip in [
                    "token extracted:", "access_token=", "refresh_token=",
                    "persistent context", "localstorage",
                ]):
                    continue
                filtered_lines.append(line)
            log_content = "\n".join(filtered_lines) if filtered_lines else log_content

    return jsonify({
        "logs": log_content,
        "is_admin": is_admin,
        "bot_running": bot_running,
    })


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 5030))
    debug = os.environ.get('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
    app.run(debug=debug, port=port, host='0.0.0.0')
