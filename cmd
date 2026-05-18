pm2 delete codementor-bot 2>/dev/null; pm2 start app.py --name codementor-bot --interpreter python3 --env FLASK_PORT=5030 2>&1
