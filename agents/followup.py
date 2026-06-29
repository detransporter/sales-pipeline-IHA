from datetime import datetime, timedelta, timezone
from database import supabase_client as db
from agents.dm_generator import generate_followup

FOLLOWUP_1_DAYS = 3   # dag 3 efter ursprungligt mejl
FOLLOWUP_2_DAYS = 4   # dag 7 totalt (4 dagar efter uppföljning 1 skickades)
CLOSE_DAYS = 7        # stäng 7 dagar efter uppföljning 2


def _days_since(ts_str: str) -> int:
    """Return number of days since an ISO timestamp string (UTC-säker)."""
    if not ts_str:
        return 0
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return 0
    # Saknar tidsstämpeln tidszon? Anta UTC så vi kan jämföra med now (tz-aware).
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - ts).days


def get_followups_due() -> list[dict]:
    """
    Return list of prospects that need action today.
    Each item: {prospect, action, message}
    action: 'followup_1' | 'followup_2' | 'close'
    """
    due = []

    # Status 'skickad' → check if followup_1 is due
    skickade = db.get_prospects(status="skickad")
    for p in skickade:
        dm = db.get_latest_dm(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= FOLLOWUP_1_DAYS:
                msg = generate_followup(p["namn"], "followup_1")
                due.append({"prospect": p, "action": "followup_1", "message": msg})

    # Status 'followup_1' → check if followup_2 is due
    f1_prospects = db.get_prospects(status="followup_1")
    for p in f1_prospects:
        dm = db.get_latest_dm(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= FOLLOWUP_2_DAYS:
                msg = generate_followup(p["namn"], "followup_2")
                due.append({"prospect": p, "action": "followup_2", "message": msg})

    # Status 'followup_2' → close after CLOSE_DAYS
    f2_prospects = db.get_prospects(status="followup_2")
    for p in f2_prospects:
        dm = db.get_latest_dm(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= CLOSE_DAYS:
                due.append({"prospect": p, "action": "close", "message": ""})

    return due


def process_close(prospect_id: str) -> None:
    """Mark a prospect as inget_svar (no more follow-ups)."""
    db.update_prospect_status(prospect_id, "inget_svar")


def get_daily_summary() -> dict:
    """Return counts for the daily Telegram briefing."""
    from database.supabase_client import get_client
    client = get_client()

    all_prospects = client.table("prospects").select("status").execute().data
    status_counts = {}
    for p in all_prospects:
        s = p["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    due = get_followups_due()
    followups_due = len([d for d in due if d["action"] in ("followup_1", "followup_2")])
    new_to_send = status_counts.get("ej_kontaktad", 0)
    awaiting = status_counts.get("skickad", 0) + status_counts.get("followup_1", 0)

    today = datetime.now(timezone.utc).date().isoformat()
    meetings_today = client.table("meetings").select("*, prospects(namn, bolag)").eq("datum", today).eq("status", "bokad").execute().data

    return {
        "new_to_send": new_to_send,
        "followups_due": followups_due,
        "awaiting_reply": awaiting,
        "meetings_today": meetings_today,
    }
