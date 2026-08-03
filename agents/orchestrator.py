"""
Lägesbild — en snabb ögonblicksbild av pipeline, för startsidan 🏠 Idag.

Filen hette och var mer förr: en "Sales Chief"-orchestrator som varje morgon
skulle förbereda DM:s, föreslå nya leads och putta en prioriteringslista till
Telegram (`run_day()` + hjälpare). Den togs bort 2026-08-03 på Davids beslut —
den gick bara att nå via ett cron-jobb som aldrig installerades och ett
Telegram-kommando (/chef) i en bot som inte kördes, så ~200 rader låg och
åldrades utan att någonsin köras eller synas. Vill du ha tillbaka den finns
den i git-historiken (commit före "Tar bort orchestratorn").

Kvar är det enda som faktiskt användes: gather_state().
"""

from database import supabase_client as db
from agents.followup import get_followups_due, get_daily_summary


def gather_state() -> dict:
    """Snabb ögonblicksbild av pipeline utan att skapa något."""
    # Hämta get_followups_due() EN gång och återanvänd — get_daily_summary()
    # gjorde tidigare om samma N+1-tunga fråga internt, vilket dubblerade
    # databasarbetet varje gång "🏠 Idag" laddades.
    due = get_followups_due()
    summary = get_daily_summary(due=due)
    stats = db.get_pipeline_stats()
    followups = [d for d in due if d["action"] in ("followup_1", "followup_2")]
    closes = [d for d in due if d["action"] == "close"]
    new_prospects = db.get_prospects(status="ej_kontaktad", min_score=5)
    pending_leads = db.get_lead_suggestions(status="pending")
    return {
        "stats": stats,
        "summary": summary,
        "followups": followups,
        "closes": closes,
        "new_prospects": new_prospects,
        "pending_leads": pending_leads,
    }
