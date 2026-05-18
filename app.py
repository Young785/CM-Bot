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
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import threading
import secrets
import time
import aiohttp
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

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
bot_processes = {}  # user_id -> process
bot_schedulers = {}  # user_id -> BackgroundScheduler
global_bot_status = {"running": False, "message": "Ready"}

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'landing'

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

def load_json_file(filepath, default=None):
    """Load JSON file with error handling"""
    if filepath.exists():
        try:
            with open(filepath) as f:
                content = f.read().strip()
                if not content:
                    return default or {}
                return json.loads(content)
        except (json.JSONDecodeError, Exception) as e:
            print(f"Warning: Could not load {filepath}: {e}")
            return default or {}
    return default or {}

def save_json_file(filepath, data):
    """Save JSON file"""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


def load_merged_tokens(user_tokens_path):
    """A1 from the user's token file; A2 from global session_tokens.json (matches the bot)."""
    user_data = load_json_file(user_tokens_path, {})
    if not isinstance(user_data, dict):
        user_data = {}
    merged = dict(user_data)
    global_data = load_json_file(TOKENS_FILE, {})
    if not isinstance(global_data, dict):
        global_data = {}
    a2 = global_data.get('A2')
    if isinstance(a2, dict) and a2.get('access_token'):
        merged['A2'] = a2
    return merged


def get_global_config():
    """Get global config (A2 is shared)"""
    return load_json_file(CONFIG_FILE, {})

def get_user_config(user_id):
    """Get user-specific config merged with global"""
    global_config = get_global_config()
    user_data = load_json_file(USER_DATA_DIR / f"{user_id}_data.json", {})
    
    # Merge configs: global for A2, user-specific for A1
    config = {
        'a2_email': global_config.get('account_a2', {}).get('email'),
        'a2_password': global_config.get('account_a2', {}).get('password'),
        'a1_email': user_data.get('a1_email'),
        'a1_password': user_data.get('a1_password'),
        'message': user_data.get('message', global_config.get('message', 'I am interested in your request')),
        'check_interval': global_config.get('check_interval_minutes', 5)
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
            login_user(user)
            
            # Set admin session if user is admin
            if user_data.get('is_admin'):
                session['is_admin'] = True
            
            # Check if onboarding is complete
            if not user_data.get('onboarding_complete'):
                flash('Please complete onboarding to continue', 'info')
                return redirect(url_for('onboarding'))
            
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
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
        
        flash('Onboarding complete! You can now run the bot.', 'success')
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
    user_bot_status = bot_processes.get(current_user.id, {}).get('status', 
                      {'running': False, 'message': 'Ready'})
    
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
    """User's requests viewer page"""
    requests_data = load_json_file(current_user.get_requests_db(), {})
    # Sort by processed_at descending
    sorted_requests = sorted(
        requests_data.values(),
        key=lambda x: x.get('processed_at', ''),
        reverse=True
    )
    return render_template('requests.html', requests=sorted_requests)

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
        current['A1']['access_token'] = data['A1'].get('access_token', '')
        if data['A1'].get('refresh_token'):
            current['A1']['refresh_token'] = data['A1'].get('refresh_token')
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

@app.route('/api/requests', methods=['GET'])
@login_required
def get_requests():
    """Get user's requests"""
    requests_data = load_json_file(current_user.get_requests_db(), {})
    return jsonify(requests_data)

@app.route('/api/requests/clear', methods=['POST'])
@login_required
def clear_requests():
    """Clear user's requests database"""
    save_json_file(current_user.get_requests_db(), {})
    return jsonify({"success": True, "message": "Requests database cleared"})

def extract_token_from_cookie_string(cookie_string):
    """Extract ACCESS_TOKEN from a full cookie string"""
    if not cookie_string:
        return None
    
    # Look for ACCESS_TOKEN= in the cookie string
    if 'ACCESS_TOKEN=' in cookie_string:
        # Split by ACCESS_TOKEN= and take the part after it
        parts = cookie_string.split('ACCESS_TOKEN=')
        if len(parts) > 1:
            # The token ends at the next semicolon or end of string
            token_part = parts[1]
            if ';' in token_part:
                return token_part.split(';')[0]
            else:
                return token_part
    
    # If the entire string is just the token (no ACCESS_TOKEN= prefix)
    if cookie_string.startswith('eyJ') or cookie_string.startswith('eyJ0'):
        return cookie_string
    
    return cookie_string  # Return as-is if we can't parse it

@app.route('/api/clients', methods=['GET'])
@login_required
def get_clients():
    """Fetch clients/contacts from Codementor API using A1 token(s)
    
    For regular users: fetches clients for their own A1 token
    For admin users: fetches clients from ALL users' A1 tokens and aggregates them
    """
    try:
        # Check if user is admin
        users = load_json_file(USERS_FILE, {})
        is_admin = users.get(current_user.id, {}).get('is_admin', False)
        session_is_admin = session.get('is_admin', False)
        
        # Get list of A1 tokens to query
        tokens_to_query = []
        
        if is_admin or session_is_admin:
            # Admin: get all users' A1 tokens
            for user_id, user_info in users.items():
                user_tokens_file = USER_DATA_DIR / f"{user_id}_tokens.json"
                if user_tokens_file.exists():
                    user_tokens = load_json_file(user_tokens_file, {})
                    # Try both 'A1' key and 'access_token' key
                    a1_token = user_tokens.get('A1', '') or user_tokens.get('access_token', '')
                    # Extract clean token if it's in cookie format
                    a1_token = extract_token_from_cookie_string(a1_token)
                    if a1_token:
                        # Get user email for display
                        user_email = user_info.get('email', user_id)
                        tokens_to_query.append({
                            'token': a1_token,
                            'account': user_email,
                            'user_id': user_id
                        })
        
        # If no admin tokens found or not admin, use current user's token
        if not tokens_to_query:
            user_tokens = load_merged_tokens(current_user.get_tokens_file())
            a1_token = user_tokens.get('A1', '') or user_tokens.get('access_token', '')
            # Extract clean token if it's in cookie format
            a1_token = extract_token_from_cookie_string(a1_token)
            if a1_token:
                tokens_to_query.append({
                    'token': a1_token,
                    'account': current_user.email,
                    'user_id': current_user.id
                })
        
        if not tokens_to_query:
            return jsonify({"success": False, "error": "A1 token not configured. Please go to Tokens page and add your ACCESS_TOKEN from Codementor cookies."})
        
        # Prepare request headers
        headers = {
            'X-Requested-From': 'cm-chat',
            'x-custom-referrer': 'https://www.codementor.io/m/dashboard/open-requests',
            'Origin': 'https://www.codementor.io',
            'Referer': 'https://www.codementor.io/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0'
        }
        
        before_timestamp = int(time.time())
        
        # Async function to fetch from one account
        async def fetch_for_account(token_info):
            cookies = {'ACCESS_TOKEN': token_info['token']}
            url = f'https://api.codementor.io/api/v2/chats/contacts?before_timestamp={before_timestamp}'
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, cookies=cookies, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            clients = await resp.json()
                            # Tag each client with the account it came from
                            for client in clients:
                                client['_source_account'] = token_info['account']
                                client['_source_user_id'] = token_info['user_id']
                            return {'success': True, 'clients': clients, 'account': token_info['account']}
                        elif resp.status == 401:
                            # Token expired - try to use cached data for this user
                            user_data_file = USER_DATA_DIR / f"{token_info['user_id']}_data.json"
                            user_data = load_json_file(user_data_file, {})
                            if 'cached_clients' in user_data:
                                return {
                                    'success': True, 
                                    'clients': user_data['cached_clients'],
                                    'account': token_info['account'],
                                    'cached': True,
                                    'error': 'Token expired, using cached data'
                                }
                            return {'success': False, 'status': 401, 'account': token_info['account'], 'error': 'Token expired'}
                        else:
                            return {'success': False, 'status': resp.status, 'account': token_info['account']}
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
        
        # Mark favorited clients
        for client in clients:
            client['favorited'] = favorites.get(client.get('username'), False)
        
        # Store aggregated clients in admin's cache
        user_data['cached_clients'] = clients
        user_data['cached_clients_time'] = datetime.now().isoformat()
        save_json_file(user_data_file, user_data)
        
        response_data = {
            "success": True, 
            "clients": clients,
            "total_accounts": len(tokens_to_query),
            "failed_accounts": failed_accounts if failed_accounts else None,
            "accounts_with_cached_data": accounts_with_cached_data if accounts_with_cached_data else None
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
                client['favorited'] = favorites.get(client.get('username'), False)
            
            return jsonify({
                "success": True, 
                "clients": clients,
                "cached": True,
                "cached_at": user_data.get('cached_clients_time', 'unknown')
            })
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
    """Create a temporary config file for the bot"""
    config = get_user_config(user_id)
    user_config_file = USER_DATA_DIR / f"{user_id}_bot_config.json"
    save_json_file(user_config_file, config)
    return user_config_file

@app.route('/api/bot/run', methods=['POST'])
@login_required
def run_bot():
    """Run the bot for current user"""
    user_id = current_user.id
    
    if user_id in bot_processes and bot_processes[user_id].get('running', False):
        return jsonify({"success": False, "message": "Bot is already running for your account"})
    
    # Check if user has completed onboarding
    users = load_json_file(USERS_FILE, {})
    if not users.get(user_id, {}).get('onboarding_complete'):
        return jsonify({"success": False, "message": "Please complete onboarding first"})
    
    try:
        # Create user-specific config
        user_config_file = create_user_config_file(user_id)
        user_log = USER_DATA_DIR / f"{user_id}.log"
        
        # Write start marker to log
        with open(user_log, 'a') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Run at {datetime.now()} for user {current_user.email}\n")
            f.write(f"{'='*60}\n")
            f.flush()
        
        bot_processes[user_id] = {'running': True, 'message': 'Starting...'}
        
        # Run bot in background with user-specific config
        def run_bot_thread():
            try:
                # Use file paths directly (not current_user - doesn't work in thread)
                user_tokens_file = USER_DATA_DIR / f"{user_id}_tokens.json"
                user_requests_file = USER_DATA_DIR / f"{user_id}_requests.json"
                
                env = os.environ.copy()
                env['USER_ID'] = user_id
                env['IS_ADMIN'] = 'true' if users.get(user_id, {}).get('is_admin') else 'false'
                env['USER_TOKENS_FILE'] = str(user_tokens_file)
                env['USER_REQUESTS_FILE'] = str(user_requests_file)
                env['USER_CONFIG_FILE'] = str(user_config_file)
                
                process = subprocess.Popen(
                    ['.venv/bin/python', 'codementor_bot_hybrid.py'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
                
                bot_processes[user_id]['process'] = process
                bot_processes[user_id]['status'] = {'running': True, 'message': 'Running...'}
                
                # Stream output to log file in real-time
                with open(user_log, 'a') as f:
                    for line in iter(process.stdout.readline, ''):
                        if line:
                            f.write(line)
                            f.flush()
                
                # Wait for process to complete
                process.wait(timeout=600)  # 10 minute timeout
                
                bot_processes[user_id]['status'] = {
                    'running': False,
                    'last_run': datetime.now().isoformat(),
                    'message': 'Completed successfully' if process.returncode == 0 else f'Error: {process.returncode}'
                }
            except subprocess.TimeoutExpired:
                process.kill()
                bot_processes[user_id]['status'] = {
                    'running': False,
                    'message': 'Timeout - bot took too long'
                }
            except Exception as e:
                bot_processes[user_id]['status'] = {
                    'running': False,
                    'message': f'Error: {str(e)}'
                }
            finally:
                bot_processes[user_id]['running'] = False
        
        thread = threading.Thread(target=run_bot_thread, daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": "Bot started - check logs for progress"})
    except Exception as e:
        bot_processes[user_id]['running'] = False
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/bot/status', methods=['GET'])
@login_required
def get_bot_status():
    """Get bot status for current user"""
    user_id = current_user.id
    status = bot_processes.get(user_id, {}).get('status', 
             {'running': False, 'message': 'Ready', 'last_run': None})
    return jsonify(status)

@app.route('/api/bot/stop', methods=['POST'])
@login_required
def stop_bot():
    """Stop the running bot for current user"""
    user_id = current_user.id
    
    if user_id not in bot_processes or not bot_processes[user_id].get('running', False):
        return jsonify({"success": False, "message": "Bot is not running"})
    
    try:
        process = bot_processes[user_id].get('process')
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
        """Execute the bot in background"""
        try:
            # Create user-specific config
            user_config_file = create_user_config_file(self.user_id)
            
            # Check if bot is already running for this user
            if self.user_id in bot_processes and bot_processes[self.user_id].get('running', False):
                self._log("[Background Scheduler] Bot already running, skipping")
                return False
            
            # Write start marker
            with open(self.user_log, 'a') as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[Background Run] {datetime.now()}\n")
                f.write(f"{'='*60}\n")
                f.flush()
            
            bot_processes[self.user_id] = {'running': True, 'message': 'Background run...'}
            
            # Use file paths directly
            user_tokens_file = USER_DATA_DIR / f"{self.user_id}_tokens.json"
            user_requests_file = USER_DATA_DIR / f"{self.user_id}_requests.json"
            
            env = os.environ.copy()
            env['USER_ID'] = self.user_id
            # Check if user is admin
            users = load_json_file(USERS_FILE, {})
            is_admin = users.get(self.user_id, {}).get('is_admin', False)
            env['IS_ADMIN'] = 'true' if is_admin else 'false'
            env['USER_TOKENS_FILE'] = str(user_tokens_file)
            env['USER_REQUESTS_FILE'] = str(user_requests_file)
            env['USER_CONFIG_FILE'] = str(user_config_file)
            
            # Run bot and capture output
            process = subprocess.Popen(
                ['.venv/bin/python', 'codementor_bot_hybrid.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
            
            bot_processes[self.user_id]['process'] = process
            bot_processes[self.user_id]['status'] = {'running': True, 'message': 'Background running...'}
            
            # Stream output to log file with timeout
            with open(self.user_log, 'a') as f:
                start_time = time.time()
                for line in iter(process.stdout.readline, ''):
                    if line:
                        f.write(line)
                        f.flush()
                    # 10 minute timeout per run
                    if time.time() - start_time > 600:
                        process.terminate()
                        break
            
            # Wait for process to complete
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            
            bot_processes[self.user_id]['status'] = {
                'running': False,
                'last_run': datetime.now().isoformat(),
                'message': 'Background completed'
            }
            bot_processes[self.user_id]['running'] = False
            
            return process.returncode == 0
            
        except Exception as e:
            self._log(f"[Background Scheduler] Bot run error: {e}")
            bot_processes[self.user_id]['running'] = False
            return False


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
        return jsonify({"success": True, "message": "Background scheduler stopped"})
    
    return jsonify({"success": False, "message": "Scheduler not running"})


@app.route('/api/scheduler/status', methods=['GET'])
@login_required
def get_scheduler_status():
    """Get background scheduler status for current user"""
    user_id = current_user.id
    
    if user_id in bot_schedulers and bot_schedulers[user_id].running:
        scheduler = bot_schedulers[user_id]
        return jsonify({
            "running": True,
            "run_count": scheduler.run_count,
            "last_run": scheduler.last_run.isoformat() if scheduler.last_run else None,
            "next_run": scheduler.next_run.isoformat() if scheduler.next_run else None,
            "interval_minutes": scheduler.check_interval // 60
        })
    
    return jsonify({
        "running": False,
        "run_count": 0,
        "last_run": None,
        "next_run": None
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
    for user_id, process_info in bot_processes.items():
        if process_info.get('running', False):
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

@app.route('/api/process/<request_id>', methods=['GET', 'POST'])
@login_required
def process_single_request(request_id):
    """Process a single request by ID - express interest and send message"""
    import aiohttp
    import asyncio
    import uuid
    
    # Load user's request data
    requests_data = load_json_file(current_user.get_requests_db(), {})
    request_data = requests_data.get(request_id)
    
    if not request_data:
        return jsonify({"success": False, "error": "Request not found in database"})
    
    # Load user's A1 token
    user_tokens = load_json_file(current_user.get_tokens_file(), {})
    a1_token = user_tokens.get('A1', {}).get('access_token', '')
    
    if not a1_token:
        return jsonify({"success": False, "error": "No A1 token available. Please login first."})
    
    # Load user config for message
    user_config = get_user_config(current_user.id)
    message = user_config.get('message', 'I am interested in your request')
    
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
    
    async def do_process():
        async with aiohttp.ClientSession() as session:
            random_key = request_data.get('random_key', request_id)
            
            # Step 1: Express Interest
            interest_url = f"https://api.codementor.io/api/v2/requests/{random_key}/interests"
            interest_payload = {
                "message": message,
                "open_to_special_rate": False
            }
            
            try:
                async with session.post(interest_url, headers=headers, cookies=cookies, json=interest_payload) as resp:
                    if resp.status in [200, 201]:
                        success = True
                    elif resp.status == 409:
                        success = True  # Already interested
                    else:
                        text = await resp.text()
                        return {"success": False, "error": f"Interest failed: {resp.status} - {text[:100]}"}
            except Exception as e:
                return {"success": False, "error": f"Interest error: {str(e)}"}
            
            # Step 2: Send Message
            username = request_data.get('author', '')
            if username and success:
                try:
                    msg_url = f"https://api.codementor.io/api/v2/chats/messages/{username}"
                    temp_id = str(uuid.uuid4())
                    msg_payload = {
                        "message": {
                            "content": message,
                            "type": "message",
                            "request": {"temp_message_id": temp_id}
                        }
                    }
                    
                    async with session.post(msg_url, headers=headers, cookies=cookies, json=msg_payload) as resp:
                        pass  # Message sent (or not), we already succeeded with interest
                except Exception:
                    pass  # Message failure doesn't invalidate interest success
            
            # Mark as processed
            request_data['processed_at'] = datetime.now().isoformat()
            requests_data[request_id] = request_data
            save_json_file(current_user.get_requests_db(), requests_data)
            
            return {"success": True, "message": "Request processed successfully"}
    
    try:
        result = asyncio.run(do_process())
        return jsonify(result)
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
    """Get recent logs for current user - filters sensitive data for non-admins"""
    user_log = USER_DATA_DIR / f"{current_user.id}.log"
    log_content = ""
    
    # Check if user is admin
    users = load_json_file(USERS_FILE, {})
    is_admin = users.get(current_user.id, {}).get('is_admin', False)
    
    if user_log.exists():
        with open(user_log) as f:
            content = f.read()
            # Return last 5000 characters
            log_content = content[-5000:] if len(content) > 5000 else content
            
            # Filter logs for non-admin users
            if not is_admin:
                filtered_lines = []
                for line in log_content.split('\n'):
                    line_lower = line.lower()
                    # Skip lines with sensitive token extraction details
                    if any(skip in line_lower for skip in [
                        'token extracted:', 'access_token', 'refresh_token',
                        ' extracting', 'token saved', 'persistent context',
                        'cookies', 'localstorage', 'extracted:'
                    ]):
                        continue
                    # Show processing logs (including A2 scanning)
                    if any(keep in line for keep in [
                        'Scanning', 'Found', 'requests from API', 'requests to process',
                        'Processing', 'Interest', 'Message sent', 'Step',
                        'Done!', 'Total stored', 'No new requests', 'Missing/Active',
                        'Opening Chrome', 'logged in', 'Login'
                    ]):
                        filtered_lines.append(line)
                log_content = '\n'.join(filtered_lines) if filtered_lines else "[Logs filtered - waiting for process output...]"
    
    return jsonify({"logs": log_content, "is_admin": is_admin})


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 5030))
    app.run(debug=True, port=port, host='0.0.0.0')
