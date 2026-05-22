# CMBOTv2 — Codementor Auth Automation

Automates Codementor.io request scanning (A2) and interest messaging (A1) using API tokens. Credentials from onboarding drive **automatic login and token refresh** — manual cookie copy is only a fallback.

## Project layout

```
CMBOTv2/
├── cmbot/                    # Core package
│   ├── auth/                 # Token load/save, refresh, Playwright login
│   │   ├── tokens.py
│   │   └── service.py        # TokenAuthService
│   ├── api/                  # Codementor REST client
│   ├── bot/
│   │   └── hybrid.py         # Scan + process workflow
│   ├── storage/              # JSON persistence
│   └── paths.py              # Paths & env (USER_ID, etc.)
├── scripts/
│   ├── refresh_auth.py       # CLI: refresh tokens from credentials
│   └── run_bot.py            # CLI: one bot cycle
├── app.py                    # Multi-user Flask UI (PM2 entry)
├── codementor_bot_hybrid.py  # Shim → cmbot.bot.hybrid
├── config.json               # Global A2 + defaults (gitignored)
├── session_tokens.json       # Global A2 tokens (gitignored)
├── user_data/                # Per-user tokens, requests, logs
└── templates/                # Web UI
```

## Quick start

```bash
cd /root/CMBOTv2
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

cp config.example.json config.json   # fill A2 scanner credentials
cp session_tokens.example.json session_tokens.json

# Refresh tokens automatically (uses email/password in config.json)
.venv/bin/python scripts/refresh_auth.py

# Run one automation cycle
.venv/bin/python scripts/run_bot.py

# Or start the web UI (auto-refresh before each bot run)
pm2 start ecosystem.config.json
```

## Auth flow (automated)

1. **Onboarding** — user enters Codementor A1 email/password; the app triggers a background token fetch.
2. **Before each bot run** — `TokenAuthService` validates tokens, refreshes via `REFRESH_TOKEN`, or logs in with stored credentials (Playwright).
3. **Scheduler** — same auto-refresh runs on each interval.
4. **Manual fallback** — Tokens page or `POST /api/auth/refresh` if automation fails (2FA, captcha).

## Configuration

`config.json` (global):

```json
{
  "account_a2": { "email": "...", "password": "..." },
  "message": "Your proposal text",
  "check_interval_minutes": 5
}
```

Per-user A1 credentials live in `users.json` / `user_data/{id}_data.json` after onboarding.

## PM2

```bash
pm2 start ecosystem.config.json   # runs app.py on port 5030
```

## Security

- Never commit `config.json`, `session_tokens.json`, `users.json`, or `user_data/`.
- Rotate admin password in `app.py` (`get_admin_password`).
