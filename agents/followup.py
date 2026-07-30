from datetime import datetime, timedelta, timezone
from database import supabase_client as db

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

    OBS: 'message' är ALLTID "" här nu. Innan genererades uppföljningstexten
    (ett Claude-anrop) för VARJE kontakt i listan, redan när den här
    funktionen kördes — och den körs på nytt vid varenda sidladdning/klick
    på 🏠 Idag och 💬 Svar & uppföljning (Streamlit kör om hela sidan för
    varje interaktion). Med 100+ kontakter i kön blev det en storm av
    Claude-anrop per sidritning — kostade i onödan (texten användes bara i
    EN av tre flikar i uppföljningskortet) och kunde trigga nätverksfel.
    Texten genereras nu bara på begäran i views/replies.py:s
    "✔️ Markera manuellt"-flik (samma knapp-mönster som ringmanuset redan
    använder) — 📧 Mejla och 📞 Ring genererar redan sin egen text på
    knapptryck och påverkas inte alls av den här ändringen.
    """
    due = []

    skickade = db.get_prospects(status="skickad")
    f1_prospects = db.get_prospects(status="followup_1")
    f2_prospects = db.get_prospects(status="followup_2")

    # EN samlad fråga för senaste dm åt ALLA kontakter, istället för en fråga
    # per kontakt i varje loop nedan (N+1 — med 200+ kontakter i dessa tre
    # statusar kunde det trigga Cloudflares skydd framför Supabase).
    all_ids = [p["id"] for p in skickade + f1_prospects + f2_prospects]
    latest_dms = db.get_latest_dms_for_prospects(all_ids)

    # Status 'skickad' → check if followup_1 is due
    for p in skickade:
        dm = latest_dms.get(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= FOLLOWUP_1_DAYS:
                due.append({"prospect": p, "action": "followup_1", "message": ""})

    # Status 'followup_1' → check if followup_2 is due
    for p in f1_prospects:
        dm = latest_dms.get(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= FOLLOWUP_2_DAYS:
                due.append({"prospect": p, "action": "followup_2", "message": ""})

    # Status 'followup_2' → close after CLOSE_DAYS
    for p in f2_prospects:
        dm = latest_dms.get(p["id"])
        if dm and dm.get("skickad_at"):
            days = _days_since(dm["skickad_at"])
            if days >= CLOSE_DAYS:
                due.append({"prospect": p, "action": "close", "message": ""})

    return due


def postpone_followup(prospect_id: str, action: str, until_date, anteckning: str = "",
                      existing_extra_info: str = "") -> None:
    """
    Skjut upp nästa kontakt till `until_date` (ett date-objekt), med en valfri
    anteckning om VARFÖR (t.ex. "Sa nej till möte nu, men gärna senare i år").

    Kontakten försvinner ur uppföljningskön och dyker upp igen på det valda
    datumet. Mekanik: lägg ett dm vars `skickad_at` ankras så att dagräkningen
    når tröskeln exakt på `until_date` (samma paus-trick som autosvar använder).
    Bra när mottagaren är på semester — eller, med en anteckning, av vilken
    annan anledning som helst.

    Anteckningen sparas (tidsstämplad) i prospects.extra_info — läggs TILL,
    skriver aldrig över tidigare anteckningar — så den syns när kontakten
    dyker upp igen, istället för att bara ligga begravd i en dm_history-rad
    som ingen vy visar. `existing_extra_info` = anroparens redan inlästa
    prospects.extra_info (anroparen har redan hela posten i minnet, så vi
    slipper en extra databasläsning här bara för det).
    """
    threshold = {
        "followup_1": FOLLOWUP_1_DAYS,
        "followup_2": FOLLOWUP_2_DAYS,
        "close": CLOSE_DAYS,
    }.get(action, FOLLOWUP_1_DAYS)
    anchor = until_date - timedelta(days=threshold)
    anteckning = (anteckning or "").strip()
    dm_text = (f"📅 Uppskjuten till {until_date.isoformat()} — {anteckning}"
               if anteckning else
               f"📅 Uppskjuten till {until_date.isoformat()} (mottagaren ej "
               f"tillgänglig, t.ex. semester).")
    dm = db.insert_dm(prospect_id, dm_text, typ="uppskjuten")
    db.mark_dm_skickad(dm["id"], at=anchor.isoformat())

    if anteckning:
        combined = db.append_note(
            existing_extra_info,
            f"Uppskjuten till {until_date.isoformat()}: {anteckning}")
        try:
            db.update_prospect(prospect_id, {"extra_info": combined})
        except Exception:
            pass  # anteckningen är en bonus — får aldrig blockera själva uppskjutningen


RECONTACT_MONTHS = 4  # hur långt fram "Stäng" automatiskt schemalägger återkontakt


def process_close(prospect_id: str) -> None:
    """
    Mark a prospect as inget_svar (no more follow-ups just now) — men sätter
    samtidigt ett återkontakts-datum ~4 månader fram istället för att kontakten
    bara försvinner ur pipeline för gott. Ren påminnelse: skickar inget.
    """
    db.update_prospect_status(prospect_id, "inget_svar")
    next_date = (datetime.now(timezone.utc).date()
                 + timedelta(days=30 * RECONTACT_MONTHS)).isoformat()
    db.set_next_contact_date(prospect_id, next_date)


def get_daily_summary(due: list[dict] | None = None) -> dict:
    """
    Return counts for the daily Telegram briefing.

    `due` kan skickas in av anroparen om den redan hämtat get_followups_due()
    (t.ex. orchestrator.gather_state()) — annars hämtas den härifrån som förut.
    Undviker att göra samma N+1-tunga fråga (en Supabase-fråga per kontakt)
    två gånger i samma sidladdning.
    """
    from database.supabase_client import get_client
    client = get_client()

    all_prospects = client.table("prospects").select("status").execute().data
    status_counts = {}
    for p in all_prospects:
        s = p["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    if due is None:
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
