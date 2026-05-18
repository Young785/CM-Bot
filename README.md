# Codementor.io Automation Bot - Hybrid Mode

This Python bot automates scanning and responding to codementor.io requests using a hybrid approach: **API for scanning** and **Browser for processing**.

## How It Works

1. **Account A2 (Scanner - API)**: Uses extracted auth tokens to scan open requests via REST API
2. **Account A1 (Interactor - Browser)**: Uses browser automation to express interest and send messages
3. **Smart Comparison**: Compares A2's found requests with A1's already-processed requests via API
4. **Processing**: For each missing/new request:
   - Opens request page in browser
   - Clicks "Get Started" / "Express Interest"
   - Types predefined message
   - Clicks "Message" button and sends message

## Quick Start

```bash
# 1. Ensure dependencies are installed
.venv/bin/pip install aiohttp playwright
.venv/bin/playwright install chromium

# 2. Configure credentials in config.json (already done)

# 3. Extract fresh tokens (see Token Extraction section)

# 4. Run the bot
.venv/bin/python codementor_bot_hybrid.py
```

## Token Extraction

Tokens expire after ~4 minutes. You need fresh tokens before each run.

**Quick Method - Manual:**
1. In Firefox (A1), press F12 → Console
2. Type: `document.cookie.match(/ACCESS_TOKEN=([^;]+)/)[1]`
3. Copy the token value
4. Repeat for Chrome (A2)
5. Edit `session_tokens.json` and paste tokens

## Project Files

| File | Purpose |
|------|---------|
| `codementor_bot_hybrid.py` | Main bot - API scanning + Browser processing |
| `config.json` | Account credentials and message |
| `session_tokens.json` | Auth tokens for API access |
| `requests_db.json` | Database of processed requests |
| `requirements.txt` | Python dependencies |

## Configuration

Edit `config.json`:

```json
{
  "account_a2": {
    "email": "kodaoluidris@gmail.com",
    "password": "Chelsea@@@111"
  },
  "account_a1": {
    "email": "tescointsite@gmail.com",
    "password": "Bismillahi11!@"
  },
  "message": "I am an experienced Software Engineer with many years experience in different stacks and i will like to show interest in your request"
}
```
    "email": "scanner_account@example.com",
    "password": "password123"
  },
  "account_a1": {
    "email": "main_account@example.com",
    "password": "password123"
  },
  "message": "Your custom message here",
  "check_interval_minutes": 5
}
```

## Test UI

A web-based testing interface is included for manual testing and debugging:

```bash
# Run the UI server
python test_ui.py

# Open http://127.0.0.1:5000 in your browser
```

### UI Features:
- **Dashboard** - Status overview, quick actions, live results
- **Test Login** - Verify both accounts can log in
- **Run Scan** - Manually trigger a request scan
- **View Requests** - Browse all stored requests in a table
- **Process Requests** - Manually process individual or all new requests
- **View Logs** - Real-time log monitoring with auto-refresh
- **Configuration** - Web form to edit config.json

### UI Pages:
- `/` - Dashboard with stats and controls
- `/requests` - Stored requests table with actions
- `/logs` - Live log viewer
- `/config` - Edit settings form

## Running as a Background Job

### On macOS/Linux (using cron):
```bash
# Edit crontab
crontab -e

# Add line to run every 10 minutes:
*/10 * * * * cd /Users/ariyoayomikun/Downloads/CMBOTv2/codementor && /usr/bin/python3 run.py --once >> bot.log 2>&1
```

### Using screen/tmux (continuous mode):
```bash
# Start in background session
tmux new-session -d -s codementor_bot 'python /Users/ariyoayomikun/Downloads/CMBOTv2/codementor/codementor_bot.py'

# Reattach to see output
tmux attach -t codementor_bot

# Detach with Ctrl+B then D
```

## Files

- `codementor_bot.py` - Main bot with all logic
- `run.py` - Convenient runner script
- `config.json` - Configuration (credentials, message, interval)
- `requests_db.json` - Auto-generated request database
- `codementor_bot.log` - Runtime logs
- `requirements.txt` - Python dependencies

## Troubleshooting

If login fails:
- Check credentials in config.json
- Look at `login_error.png` or `login_debug.png` screenshots
- Codementor may require 2FA - you'll need to complete it manually first

If request parsing fails:
- The site layout may have changed
- Check `requests_error.png` screenshot
- The bot uses multiple selector strategies for resilience

## Security Notes

- Keep your `config.json` secure (contains passwords)
- Don't commit it to git
- Run in a private environment
# CM-Bot
