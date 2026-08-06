"""
Jämför utfall mellan olika versioner av mejltexten.

Varje skickat mejl stämplas med `email_writer.PROMPT_VERSION` i
`dm_history.angle` (se views/shared.py:log_sent_email). Mejl skickade före
2026-08-06 saknar stämpel och räknas som v1.

VARFÖR DET HÄR ÄR SVÅRARE ÄN DET SER UT
Appen ser inte svar automatiskt — David läser dem i Outlook, och `svar_at` i
dm_history är satt noll gånger på 435 utskick. Det enda spår ett svar lämnar i
databasen är att David flyttar kontaktens status (svar_ja, svar_nej,
mote_bokat, avbojd). Utfallet nedan mäter alltså vad David HUNNIT registrera,
inte vad som faktiskt kommit in. Skickar han 20 nya mejl och inte rör
statusarna ser den nya versionen ut att prestera noll.

DÄRFÖR RAPPORTERAS MOGNAD SEPARAT: en version som är tre dagar gammal kan inte
jämföras med en som haft fem veckor på sig. De flesta svar kommer inom en
dryg vecka, och uppföljningskedjan är sju dagar lång — under ~10 dagar säger
siffrorna ingenting, hur lockande de än ser ut. `mogen` är False då, och den
som visar resultatet ska säga det rakt ut istället för att visa en andel som
inbjuder till fel slutsats.
"""

from datetime import datetime, timezone

from database import supabase_client as db

# Statusar som bara uppstår för att någon hört av sig (eller David avfört dem
# efter kontakt). 'inget_svar' räknas INTE — det är frånvaro av utfall.
_SVARSSTATUSAR = {"svar_ja", "svar_nej", "mote_bokat", "avbojd"}
_VUNNA = {"mote_bokat", "svar_ja"}

# Under så här många dagar är en version för ung för att jämföras: 7 dagar
# uppföljningskedja + några dagars svarstid.
MOGEN_EFTER_DAGAR = 10

V1 = "v1 (ostämplad, före 2026-08-06)"


def _dagar_sedan(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def jamfor_versioner() -> list[dict]:
    """
    En rad per mejlversion, äldst först. Varje rad:
      version, mejl, kontakter, svar, moten, svarsandel_pct, motesandel_pct,
      aldsta_dagar, yngsta_dagar, mogen
    Andelarna räknas per KONTAKT, inte per mejl — en kontakt som fått tre
    uppföljningar ska väga lika tungt som en som fått ett mejl.
    """
    sent = [e for e in db.get_sent_emails(limit=5000) if (e.get("typ") or "") == "email"]
    prospects = {p["id"]: p for p in db.get_prospects()}

    per_version: dict[str, dict] = {}
    for e in sent:
        v = e.get("angle") or V1
        rad = per_version.setdefault(v, {"version": v, "mejl": 0, "kontakter": set(),
                                         "dagar": []})
        rad["mejl"] += 1
        pid = e.get("prospect_id")
        if pid in prospects:
            rad["kontakter"].add(pid)
        d = _dagar_sedan(e.get("skickad_at") or e.get("created_at"))
        if d is not None:
            rad["dagar"].append(d)

    resultat = []
    for rad in per_version.values():
        ids = rad["kontakter"]
        statusar = [prospects[i]["status"] for i in ids]
        svar = sum(1 for s in statusar if s in _SVARSSTATUSAR)
        moten = sum(1 for s in statusar if s in _VUNNA)
        n = len(ids) or 1
        aldsta = max(rad["dagar"]) if rad["dagar"] else 0.0
        resultat.append({
            "version": rad["version"],
            "mejl": rad["mejl"],
            "kontakter": len(ids),
            "svar": svar,
            "moten": moten,
            "svarsandel_pct": round(svar / n * 100, 1),
            "motesandel_pct": round(moten / n * 100, 1),
            "aldsta_dagar": round(aldsta, 1),
            "yngsta_dagar": round(min(rad["dagar"]), 1) if rad["dagar"] else 0.0,
            # Mogen först när det ÄLDSTA mejlet i versionen hunnit få svar.
            "mogen": aldsta >= MOGEN_EFTER_DAGAR,
        })
    resultat.sort(key=lambda r: r["aldsta_dagar"], reverse=True)
    return resultat
