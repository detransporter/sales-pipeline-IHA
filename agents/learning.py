"""
Inlärnings-agent — "vad funkar?"

Tittar bakåt på all DM-historik och kopplar varje skickat DM till utfallet på
kontakten (svar / möte). Räknar ut vilken vinkel (variant) och vilken bransch
som ger bäst respons, och formulerar en kort brief som dm_generator använder
för att skriva bättre DM:s nästa gång. Detta är feedback-loopen.

Vinklar (samma som dm_generator):
  a = kortast möjliga
  b = med bolagsreferens
  c = med branschinsikt
"""

from database import supabase_client as db

# Utfall som räknas som positivt respektive avgjort
POSITIVE = {"svar_ja", "mote_bokat"}
DECIDED = {"svar_ja", "mote_bokat", "svar_nej", "inget_svar"}

ANGLE_LABELS = {
    "a": "kort & rakt på",
    "b": "med bolagsreferens",
    "c": "med branschinsikt",
}

DEFAULT_ANGLE = "b"          # bästa gissning innan vi har data
MIN_SAMPLE = 4               # minst så många skickade innan vi litar på en siffra


def _rate(positive: int, decided: int) -> float:
    return round(positive / decided * 100, 1) if decided else 0.0


def analyze_what_works() -> dict:
    """
    Returnerar:
      {
        "best_angle": "b",
        "angle_stats": {"a": {...}, "b": {...}, "c": {...}},
        "bransch_stats": {"manufacturing": {...}, ...},
        "total_decided": int,
        "brief": "kort text att mata in i dm_generator",
        "enough_data": bool,
      }
    """
    try:
        prospects = db.get_prospects()
        dms = db.get_all_dm_history(typ="initial")
    except Exception:
        return _empty_result("Ingen historik tillgänglig ännu.")

    by_id = {p["id"]: p for p in prospects}

    angle_stats: dict[str, dict] = {}
    bransch_stats: dict[str, dict] = {}
    total_decided = 0

    for dm in dms:
        angle = (dm.get("angle") or "").strip().lower() or "okänd"
        prospect = by_id.get(dm.get("prospect_id"))
        if not prospect:
            continue
        status = prospect.get("status", "")
        if status not in DECIDED:
            continue  # ännu inte avgjort — räknas inte
        total_decided += 1
        is_pos = status in POSITIVE

        a = angle_stats.setdefault(angle, {"sent": 0, "positive": 0})
        a["sent"] += 1
        a["positive"] += int(is_pos)

        bransch = (prospect.get("bransch") or "okänd").strip().lower()
        b = bransch_stats.setdefault(bransch, {"sent": 0, "positive": 0})
        b["sent"] += 1
        b["positive"] += int(is_pos)

    for stats in (angle_stats, bransch_stats):
        for d in stats.values():
            d["rate"] = _rate(d["positive"], d["sent"])

    # Bästa vinkel: högst svarsfrekvens bland de med tillräckligt underlag
    candidates = {a: d for a, d in angle_stats.items()
                  if a in ANGLE_LABELS and d["sent"] >= MIN_SAMPLE}
    enough_data = bool(candidates)
    if candidates:
        best_angle = max(candidates, key=lambda a: candidates[a]["rate"])
    else:
        best_angle = DEFAULT_ANGLE

    brief = _build_brief(best_angle, angle_stats, bransch_stats, enough_data, total_decided)

    return {
        "best_angle": best_angle,
        "angle_stats": angle_stats,
        "bransch_stats": bransch_stats,
        "total_decided": total_decided,
        "brief": brief,
        "enough_data": enough_data,
    }


def _build_brief(best_angle, angle_stats, bransch_stats, enough_data, total_decided) -> str:
    if not enough_data:
        return (
            "Ännu för lite utfallsdata för att veta säkert vad som funkar — "
            f"({total_decided} avgjorda hittills). Använd standardvinkeln "
            f"'{ANGLE_LABELS[DEFAULT_ANGLE]}' och variera lätt för att samla lärdom."
        )

    parts = [f"Bäst respons hittills: vinkeln '{ANGLE_LABELS.get(best_angle, best_angle)}'."]
    for a in ("a", "b", "c"):
        if a in angle_stats and angle_stats[a]["sent"]:
            d = angle_stats[a]
            parts.append(f"  - {ANGLE_LABELS[a]}: {d['rate']}% av {d['sent']} skickade gav svar/möte.")

    # Toppbranscher
    ranked = sorted(
        (b for b in bransch_stats.items() if b[1]["sent"] >= 2),
        key=lambda x: x[1]["rate"], reverse=True,
    )[:3]
    if ranked:
        top = ", ".join(f"{name} ({d['rate']}%)" for name, d in ranked)
        parts.append(f"Branscher som svarar bäst: {top}.")

    parts.append("Luta texten åt det som fungerar, men håll tonen kort och med en enkel ja/nej-fråga.")
    return "\n".join(parts)


def _empty_result(msg: str) -> dict:
    return {
        "best_angle": DEFAULT_ANGLE,
        "angle_stats": {},
        "bransch_stats": {},
        "total_decided": 0,
        "brief": msg,
        "enough_data": False,
    }
