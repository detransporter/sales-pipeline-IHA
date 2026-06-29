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

from database import supabase_client as db
from integrations import linkedin_inbox as inbox
from agents.qualifier import qualify_reply, CATEGORY_TO_STATUS
from agents import conversation
from brain import memory as brain


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
