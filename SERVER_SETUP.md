# Server Setup Guide

## Option 1: PM2 (Auto-start, Production-like)

PM2 runs the app as a background service that auto-starts on boot.

### Install PM2 globally:
```bash
npm install -g pm2
```

### Start the app on port 5030:
```bash
cd /Users/ariyoayomikun/Downloads/CMBOTv2
pm2 start ecosystem.config.json
```

### Save PM2 config to auto-start on boot:
```bash
pm2 save
pm2 startup  # Follow the command it outputs
```

### Useful PM2 commands:
```bash
pm2 status              # Check status
pm2 logs codementor-bot    # View logs
pm2 restart codementor-bot # Restart app
pm2 stop codementor-bot     # Stop app
pm2 delete codementor-bot   # Remove from PM2
```

Access at: **http://127.0.0.1:5030**

---

## Option 2: Laravel Valet (Mac only, Pretty URLs)

Valet provides clean URLs like `codementor-bot.test` instead of ports.

### Install Valet:
```bash
composer global require laravel/valet
valet install
```

### Link your project:
```bash
cd /Users/ariyoayomikun/Downloads/CMBOTv2
valet link codementor-bot
valet secure  # Optional: adds HTTPS
```

### Custom Port (5030):
Create `LocalValetDriver.php` in project root:
```php
<?php
class LocalValetDriver extends LaravelValetDriver
{
    public function serves($sitePath, $siteName, $uri)
    {
        return $siteName === 'codementor-bot';
    }

    public function frontControllerPath($sitePath, $siteName, $uri)
    {
        return $sitePath . '/app.py';
    }
}
```

Or use the built-in proxy:
```bash
# First start your app on port 5030
.venv/bin/python app.py &

# Then proxy it
valet proxy codementor-bot http://127.0.0.1:5030
```

Access at: **https://codementor-bot.test** (proxied to port 5030)

### Valet commands:
```bash
valet list               # Show all linked sites
valet unlink codementor-bot  # Remove link
valet unproxy codementor-bot # Remove proxy
```

---

## Quick Start (Choose One)

**For auto-start always running in background:**
```bash
pm2 start ecosystem.config.json
pm2 save
pm2 startup
```

**For pretty local URL with Valet:**
```bash
# Terminal 1: Start the app
.venv/bin/python app.py  # Runs on port 5001 or set FLASK_PORT=5030

# Terminal 2: Proxy it
valet proxy codementor http://127.0.0.1:5001
```

Then access: **https://codementor.test**
