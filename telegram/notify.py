"""
Lättviktig Telegram-push via Bot API (requests) — ingen async, lätt att köra
från cron/run_daily.py. Den fullständiga interaktiva boten finns i bot.py.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(text: str) -> bool:
    """Skicka ett meddelande till den konfigurerade chatten. Returnerar True vid lyckat."""
    if not TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False
