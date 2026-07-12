"""
Inkorg-agent — upptäcker vem som svarat och förbereder allt åt David.
SKICKAR ALDRIG något själv. Den bara läser, förstår och flaggar.

För varje nytt inkommande LinkedIn-svar:
  1. Matchar svaret mot rätt prospect (LinkedIn-URL först, annars namn)
  2. Kvalificerar svaret (intresserad / inte nu / inte relevant / boka möte)
  3. Genererar redan ett förslag på Davids nästa meddelande (samtalsläget)
  4. Lägger det i svarskön (inbox_replies) + uppdaterar prospect-status
  5. Loggar till minnet

David öppnar sen appen, läser och trycker skicka. Inget missas, ingen inklistring.
"""

import re
from datetime import date, timedelta

from database import supabase_client as db
from integrations import linkedin_inbox as inbox
from agents.qualifier import qualify_reply, CATEGORY_TO_STATUS
from agents import conversation
from brain import memory as brain


# ── Autosvar (out-of-office) ─────────────────────────────────────────────────
# Autosvar ska INTE kvalificeras eller besvaras — ingen läser svar på autosvar.
# Istället: pausa uppföljningen till återkomstdatumet och markera som hanterat.

_AUTOREPLY_PATTERNS = [
    r"\bsemester\b", r"\bvacation\b", r"\bout of office\b", r"\bautosvar\b",
    r"\bautomatic reply\b", r"\bautomatiskt svar\b", r"\bfrånvarande\b",
    r"\bföräldraledig\b", r"\bparental leave\b", r"\båter den\b", r"\båter \d",
    r"\btillbaka den\b", r"\bi'?m away\b", r"\bwill be back\b", r"\bpå ledighet\b",
]
_AUTOREPLY_RE = re.compile("|".join(_AUTOREPLY_PATTERNS), re.IGNORECASE)

_SV_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
    # engelska också — svenska autosvar är ofta tvåspråkiga
    "january": 1, "february": 2, "march": 3, "june": 6, "july": 7,
    "august": 8, "october": 10,
}

_DEFAULT_PAUSE_DAYS = 14  # hittas inget datum: pausa två veckor


def is_autoreply(text: str) -> bool:
    return bool(_AUTOREPLY_RE.search(text or ""))


def parse_return_date(text: str) -> date:
    """Försök läsa ut återkomstdatumet ur ett autosvar. Fallback: +14 dagar.
    Klarar '3:e Augusti', '3 augusti', 'August 3rd', '2026-08-03', '3/8'."""
    text = text or ""
    today = date.today()

    def _future(d: date) -> date:
        # Datum utan år: har det redan passerat i år antas nästa år.
        return d if d >= today else date(d.year + 1, d.month, d.day)

    # ISO: 2026-08-03
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Svenskt/engelskt: '3:e augusti', '3 augusti', 'august 3rd', '3rd of august'
    month_names = "|".join(_SV_MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?::e|st|nd|rd|th)?(?:\s+of)?\s+({month_names})\b",
                  text, re.IGNORECASE)
    if not m:
        m2 = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
                       text, re.IGNORECASE)
        if m2:
            m = m2
            day, month_name = m2.group(2), m2.group(1)
        else:
            day = month_name = None
    else:
        day, month_name = m.group(1), m.group(2)
    if day and month_name:
        try:
            return _future(date(today.year, _SV_MONTHS[month_name.lower()], int(day)))
        except (ValueError, KeyError):
            pass
    # Numeriskt: 3/8
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if m:
        try:
            return _future(date(today.year, int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return today + timedelta(days=_DEFAULT_PAUSE_DAYS)


def _handle_autoreply(prospect: dict, text: str, external_id: str | None,
                      run_id: str | None) -> dict:
    """Pausa uppföljning till återkomstdatum + spara svaret som redan hanterat."""
    atergang = parse_return_date(text)
    # Paus-mekanik: ett dm med skickad_at i FRAMTIDEN → followup räknar
    # dagar därifrån, så nästa uppföljning triggas ~3 dagar efter återkomst.
    dm = db.insert_dm(
        prospect["id"],
        f"🏖 Autosvar — borta till {atergang.isoformat()}. Uppföljning pausad.",
        typ="autosvar")
    db.mark_dm_skickad(dm["id"], at=atergang.isoformat())

    record = {
        "prospect_id": prospect["id"],
        "sender_name": prospect.get("namn", ""),
        "sender_url": prospect.get("linkedin_url", ""),
        "text": text,
        "kategori": "AUTOSVAR",
        "suggested_reply": "",
        "steg": prospect.get("samtal_steg", ""),
        "handled": True,   # hamnar aldrig i kön — inget för David att göra
        "external_id": external_id,
        "run_id": run_id,
    }
    try:
        saved = db.insert_inbox_reply(record)
    except Exception:
        saved = record
    db.log_action(run_id, "inbox_watcher",
                  f"Autosvar från {prospect.get('namn', '?')} — pausad till {atergang}",
                  prospect_id=prospect["id"])
    saved["_prospect"] = prospect
    saved["_atergang"] = atergang.isoformat()
    return saved


def _suggest_next(prospect: dict, text: str) -> dict:
    """Kör samtalsmotorn: bygg avskrift, läs av steget, skriv nästa drag.
    Returnerar {'meddelande', 'nuvarande_steg', 'nasta_steg'} (tål fel)."""
    try:
        historik = conversation.build_history(prospect["id"])
        return conversation.next_move(
            prospect["namn"], prospect.get("titel", ""), prospect.get("bolag", ""),
            text, historik=historik, nuvarande_steg=prospect.get("samtal_steg", ""),
        )
    except Exception:
        return {"meddelande": "", "nuvarande_steg": prospect.get("samtal_steg", ""),
                "nasta_steg": prospect.get("samtal_steg", "")}


def _match_prospect(msg: dict) -> dict | None:
    """Hitta rätt prospect för ett inkommande meddelande."""
    p = db.find_prospect_by_url(msg.get("sender_url", ""))
    if p:
        return p
    name = msg.get("sender_name", "")
    if name:
        return db.get_prospect_by_name(name)
    return None


def check_inbox(run_id: str | None = None, limit: int = 50) -> dict:
    """
    Kolla inkorgen, behandla nya svar. Returnerar:
      {"new_replies": [...], "skipped": int, "unmatched": int, "configured": bool}
    """
    if not inbox.is_configured():
        return {"new_replies": [], "skipped": 0, "unmatched": 0, "configured": False}

    messages = inbox.fetch_inbound_replies(limit=limit)
    new_replies = []
    skipped = 0
    unmatched = 0

    for msg in messages:
        prospect = _match_prospect(msg)
        if not prospect:
            unmatched += 1
            continue

        # Dedup — har vi redan sett det här svaret?
        if db.reply_exists(msg.get("external_id"), prospect["id"], msg["text"]):
            skipped += 1
            continue

        # Autosvar? Pausa uppföljningen och hoppa över kvalificeringen.
        if is_autoreply(msg["text"]):
            _handle_autoreply(prospect, msg["text"], msg.get("external_id"), run_id)
            skipped += 1
            continue

        # Kvalificera svaret
        try:
            analysis = qualify_reply(msg["text"])
            kategori = analysis.get("kategori", "")
        except Exception:
            analysis, kategori = {}, ""

        # Förbered Davids nästa meddelande via samtalsmotorn (säljtrappan)
        move = _suggest_next(prospect, msg["text"])
        suggested = move["meddelande"]

        record = {
            "prospect_id": prospect["id"],
            "sender_name": msg.get("sender_name", ""),
            "sender_url": msg.get("sender_url", ""),
            "text": msg["text"],
            "received_at": msg.get("received_at"),
            "kategori": kategori,
            "suggested_reply": suggested,
            "steg": move["nasta_steg"],
            "handled": False,
            "external_id": msg.get("external_id") or None,
            "run_id": run_id,
        }
        try:
            saved = db.insert_inbox_reply(record)
        except Exception:
            saved = record

        # Uppdatera prospect-status enligt kvalificeringen
        new_status = CATEGORY_TO_STATUS.get(kategori)
        if new_status:
            try:
                db.update_prospect_status(prospect["id"], new_status)
            except Exception:
                pass

        # Flytta kontakten i säljtrappan
        try:
            db.update_prospect_stage(prospect["id"], move["nasta_steg"])
        except Exception:
            pass

        db.log_action(run_id, "inbox_watcher", f"Nytt svar från {prospect['namn']} ({kategori})",
                      prospect_id=prospect["id"], detail={"kategori": kategori})

        saved["_prospect"] = prospect
        new_replies.append(saved)

    if new_replies:
        names = ", ".join(r["_prospect"]["namn"] for r in new_replies)
        try:
            brain.capture_thought(
                f"INKORG – {len(new_replies)} nya svar: {names}. "
                f"Förslag på svar förberedda i appen."
            )
        except Exception:
            pass

    return {
        "new_replies": new_replies,
        "skipped": skipped,
        "unmatched": unmatched,
        "configured": True,
    }


def process_manual_reply(prospect_id: str, text: str, run_id: str | None = None) -> dict:
    """
    Behandla ett INKLISTRAT svar (gratis-väg, ingen Unipile).
    Kvalificerar, skriver förslag på nästa meddelande, lägger i kön och
    uppdaterar pipeline-status. Returnerar den sparade posten.
    """
    text = (text or "").strip()
    prospect = db.get_prospects()  # hämta för att hitta namn/titel/bolag
    prospect = next((p for p in prospect if p["id"] == prospect_id), None)
    if not prospect or not text:
        raise ValueError("Kontakt eller text saknas.")

    # Autosvar? Pausa uppföljningen — inget att kvalificera eller besvara.
    if is_autoreply(text):
        return _handle_autoreply(prospect, text, None, run_id)

    try:
        analysis = qualify_reply(text)
        kategori = analysis.get("kategori", "")
    except Exception:
        kategori = ""

    move = _suggest_next(prospect, text)
    suggested = move["meddelande"]

    record = {
        "prospect_id": prospect_id,
        "sender_name": prospect["namn"],
        "sender_url": prospect.get("linkedin_url", ""),
        "text": text,
        "kategori": kategori,
        "suggested_reply": suggested,
        "steg": move["nasta_steg"],
        "handled": False,
        "external_id": None,
        "run_id": run_id,
    }
    saved = db.insert_inbox_reply(record)

    new_status = CATEGORY_TO_STATUS.get(kategori)
    if new_status:
        try:
            db.update_prospect_status(prospect_id, new_status)
        except Exception:
            pass

    try:
        db.update_prospect_stage(prospect_id, move["nasta_steg"])
    except Exception:
        pass

    db.log_action(run_id, "inbox_watcher", f"Manuellt svar behandlat ({kategori})",
                  prospect_id=prospect_id, detail={"kategori": kategori})
    saved["_prospect"] = prospect
    return saved


def build_telegram_text(result: dict) -> str:
    n = result["new_replies"]
    if not result["configured"]:
        return "Inkorg-agenten är inte kopplad än (Unipile saknas i .env)."
    if not n:
        return "📭 Inga nya LinkedIn-svar."
    lines = [f"💬 *{len(n)} nya svar på LinkedIn* — förslag klara i appen", ""]
    for r in n:
        p = r["_prospect"]
        kat = r.get("kategori") or "?"
        lines.append(f"• *{p['namn']}* @ {p.get('bolag', '')} — _{kat}_")
    lines.append("")
    lines.append("Öppna appen → 💬 Inkorg för att läsa & skicka.")
    return "\n".join(lines)
