"""
Inkorg-koll — körs ofta (t.ex. var 15:e minut) för att fånga nya LinkedIn-svar.

    python run_inbox.py

Läser inkorgen (read-only via Unipile), förbereder förslag i appen och pingar
Telegram om något nytt kommit in. Skickar aldrig något själv.

Cron-exempel (var 15:e minut, vardagar 07–20):
    */15 7-20 * * 1-5 cd /Users/davidleifsson/sales/linkedin_dm_agent && /usr/bin/python3 run_inbox.py >> inbox.log 2>&1
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from agents.inbox_watcher import check_inbox, build_telegram_text
from telegram.notify import send_telegram


def main() -> None:
    result = check_inbox()
    if not result["configured"]:
        print("Unipile inte konfigurerat — sätt UNIPILE_* i .env. Hoppar över.")
        return

    n = len(result["new_replies"])
    print(f"Inkorg-koll klar. Nya svar: {n}, "
          f"redan sedda: {result['skipped']}, omatchade: {result['unmatched']}")

    if n:
        send_telegram(build_telegram_text(result))
        print("📲 Pingade Telegram om nya svar.")


if __name__ == "__main__":
    main()
