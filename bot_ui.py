#!/usr/bin/env python3
"""
Codementor Bot Web UI
Manage the bot through a web interface
"""

import json
import os
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for
import threading
import time

app = Flask(__name__)

# File paths
TOKENS_FILE = Path("session_tokens.json")
CONFIG_FILE = Path("config.json")
REQUESTS_DB = Path("requests_db.json")
LOG_FILE = Path("bot_ui.log")

# Bot process tracking
bot_process = None
bot_status = {"running": False, "paused": False, "last_run": None, "message": "Ready"}
bot_pause_event = threading.Event()  # Event to control pausing


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


@app.route('/')
def dashboard():
    """Main dashboard"""
    tokens = load_json_file(TOKENS_FILE, {})
    config = load_json_file(CONFIG_FILE, {})
    requests_data = load_json_file(REQUESTS_DB, {})
    
    token_status = {}
    for acc in ['A1', 'A2']:
        token = tokens.get(acc, {}).get('access_token', '')
        token_status[acc] = {
            'has_token': bool(token),
            'token_preview': token[:20] + '...' if len(token) > 20 else token
        }
    
    return render_template('dashboard.html',
                         token_status=token_status,
                         bot_status=bot_status,
                         total_requests=len(requests_data),
                         config=config)


@app.route('/tokens')
def tokens_page():
    """Token management page"""
    tokens = load_json_file(TOKENS_FILE, {})
    return render_template('tokens.html', tokens=tokens)


@app.route('/api/tokens', methods=['GET'])
def get_tokens():
    """Get current tokens"""
    tokens = load_json_file(TOKENS_FILE, {})
    # Mask full tokens for security
    safe_tokens = {}
    for acc, data in tokens.items():
        safe_tokens[acc] = {
            'has_token': bool(data.get('access_token')),
            'preview': data.get('access_token', '')[:20] + '...' if data.get('access_token') else '',
            'email': data.get('user_email', '')
        }
    return jsonify(safe_tokens)


@app.route('/api/tokens', methods=['POST'])
def update_tokens():
    """Update tokens"""
    data = request.json
    current = load_json_file(TOKENS_FILE, {})
    
    for acc in ['A1', 'A2']:
        if acc in data and data[acc].get('access_token'):
            current[acc] = {
                'access_token': data[acc]['access_token'],
                'refresh_token': data[acc].get('refresh_token', ''),
                'user_email': data[acc].get('email', current.get(acc, {}).get('user_email', ''))
            }
    
    save_json_file(TOKENS_FILE, current)
    return jsonify({"success": True, "message": "Tokens updated"})


@app.route('/config')
def config_page():
    """Configuration page"""
    config = load_json_file(CONFIG_FILE, {})
    return render_template('config.html', config=config)


@app.route('/api/config', methods=['POST'])
def update_config():
    """Update configuration"""
    data = request.json
    save_json_file(CONFIG_FILE, data)
    return jsonify({"success": True, "message": "Configuration saved"})


@app.route('/requests')
def requests_page():
    """Requests viewer page"""
    requests_data = load_json_file(REQUESTS_DB, {})
    # Sort by processed_at descending and extract just the values
    sorted_requests = sorted(
        requests_data.values(),
        key=lambda x: x.get('processed_at', ''),
        reverse=True
    )
    return render_template('requests.html', requests=sorted_requests)


@app.route('/api/requests', methods=['GET'])
def get_requests():
    """Get all requests"""
    requests_data = load_json_file(REQUESTS_DB, {})
    return jsonify(requests_data)


@app.route('/api/requests/clear', methods=['POST'])
def clear_requests():
    """Clear requests database"""
    save_json_file(REQUESTS_DB, {})
    return jsonify({"success": True, "message": "Requests database cleared"})


@app.route('/api/bot/run', methods=['POST'])
def run_bot():
    """Run the bot once - non-blocking with streaming logs"""
    global bot_process, bot_status
    
    if bot_status["running"]:
        return jsonify({"success": False, "message": "Bot is already running"})
    
    try:
        bot_status["running"] = True
        bot_status["message"] = "Running..."
        
        # Write start marker to log
        with open(LOG_FILE, 'a') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Run at {datetime.now()}\n")
            f.write(f"{'='*60}\n")
            f.flush()
        
        # Run bot in background with Popen (non-blocking)
        def run_bot_thread():
            global bot_status
            try:
                # Use Popen to stream output in real-time without blocking
                process = subprocess.Popen(
                    ['.venv/bin/python', 'codementor_bot_hybrid.py'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1  # Line buffered
                )
                
                # Stream output to log file in real-time
                with open(LOG_FILE, 'a') as f:
                    for line in iter(process.stdout.readline, ''):
                        if line:
                            f.write(line)
                            f.flush()
                
                # Wait for process to complete
                process.wait(timeout=600)  # 10 minute timeout
                
                bot_status["last_run"] = datetime.now().isoformat()
                bot_status["message"] = "Completed successfully" if process.returncode == 0 else f"Error: {process.returncode}"
            except subprocess.TimeoutExpired:
                process.kill()
                bot_status["message"] = "Timeout - bot took too long"
            except Exception as e:
                bot_status["message"] = f"Error: {str(e)}"
            finally:
                bot_status["running"] = False
        
        thread = threading.Thread(target=run_bot_thread, daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": "Bot started - check logs for progress"})
    except Exception as e:
        bot_status["running"] = False
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/bot/status', methods=['GET'])
def get_bot_status():
    """Get bot status"""
    return jsonify(bot_status)


@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    """Stop the running bot"""
    global bot_process, bot_status
    
    if not bot_status["running"]:
        return jsonify({"success": False, "message": "Bot is not running"})
    
    try:
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
            try:
                bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot_process.kill()
                bot_process.wait()
        
        bot_status["running"] = False
        bot_status["paused"] = False
        bot_status["message"] = "Stopped by user"
        bot_pause_event.set()  # Clear any pause state
        
        return jsonify({"success": True, "message": "Bot stopped"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/bot/pause', methods=['POST'])
def pause_bot():
    """Pause/Resume the bot"""
    global bot_status
    
    if not bot_status["running"]:
        return jsonify({"success": False, "message": "Bot is not running"})
    
    try:
        if bot_status["paused"]:
            # Resume
            bot_pause_event.set()
            bot_status["paused"] = False
            bot_status["message"] = "Running..."
            return jsonify({"success": True, "message": "Bot resumed"})
        else:
            # Pause
            bot_pause_event.clear()
            bot_status["paused"] = True
            bot_status["message"] = "Paused"
            return jsonify({"success": True, "message": "Bot paused"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/logs')
def logs_page():
    """View logs page"""
    log_content = ""
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            log_content = f.read()
    return render_template('logs.html', logs=log_content)


@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get recent logs"""
    log_content = ""
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            content = f.read()
            # Return last 5000 characters
            log_content = content[-5000:] if len(content) > 5000 else content
    return jsonify({"logs": log_content})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
