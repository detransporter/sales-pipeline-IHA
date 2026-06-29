"""
Daglig orchestrator-körning — körs av cron (eller manuellt i terminalen).

    python run_daily.py

Kör Sales Chief, förbereder dagens jobb, loggar till Supabase + Open Brain och
puttar en prioriterad lista till Telegram. Tänkt att schemaläggas varje morgon.

Exempel på cron (kör 07:30 varje vardag) — kör `crontab -e` och lägg in:
    30 7 * * 1-5 cd /Users/davidleifsson/sales/linkedin_dm_agent && /usr/bin/python3 run_daily.py >> orchestrator.log 2>&1
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from agents.orchestrator import run_day
from telegram.notify import send_telegram


def main() -> None:
    print("Sales Chief: kör dagens orchestrering...")
    result = run_day(run_type="daily")

    text = result["telegram"]
    print(text)

    sent = send_telegram(text)
    if sent:
        print("✅ Skickat till Telegram.")
    else:
        print("ℹ️ Telegram inte konfigurerat (TELEGRAM_BOT_TOKEN/CHAT_ID saknas) "
              "— planen kördes ändå och loggades.")

    s = result["summary"]
    print(f"Klart. DM: {s['prepared_dms']}, uppföljningar: {s['prepared_followups']}, "
          f"nya leads: {s['new_leads']}, körnings-id: {result['run_id']}")


if __name__ == "__main__":
    main()
