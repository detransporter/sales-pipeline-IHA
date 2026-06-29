"""
Lead-finder-agent — föreslår NYA leads som matchar IHA:s idealkund.

Två lägen, automatiskt valda:

1. RIKTIG RESEARCH (om APIFY_TOKEN finns i .env) — kör Apifys Google Maps-scraper
   och hämtar VERKLIGA svenska bolag. Claude väljer sedan de bästa kandidaterna ur
   den listan och annoterar bransch/roll/motivering. Inga påhittade bolag.

2. AI-GISSNING (fallback om Apify saknas) — Claude föreslår bolag ur sin kunskap.
   Snabbt att komma igång men kan innehålla felaktiga/föråldrade bolag.

I båda fallen lämnas personens namn tomt — David hittar rätt person på LinkedIn.
Förslagen sparas som 'pending' i lead_suggestions; David godkänner innan kontakt.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

from agents.prospecting import score_prospect
from integrations import apify_research as apify

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Gemensam ICP-beskrivning som båda lägena delar.
ICP_BLOCK = """Produkten heter IHA — ett lättviktigt SaaS-verktyg för "Inventory Health Analysis" som visar
SME-bolag hur mycket kapital de binder i för stort eller dött lager (DOS × inköpsvärde × antal
= kapitalbindning, ca 20% årlig kostnad). Pris: IHA Essential 45 000 kr.

IDEALKUND (ICP):
- Svenskt/nordiskt SME, ca 20–200 anställda
- Tillverkare, distributör, grossist eller e-handel med FYSISKT lager och många artiklar
- Branscher som binder mycket kapital: tillverkning, industriell utrustning, automotive,
  livsmedel, medtech, kemi, plast, möbler, förpackning, bygggrossist, flyg/inflight catering
- Roller att kontakta: Supply Chain Manager, Inköpschef, Logistikchef, Operations Manager,
  CFO/Ekonomichef, VD (i mindre bolag)
- UNDVIK rena tjänstebolag, konsultbolag, mjukvarubolag, butiker/restauranger och stora
  koncerner utan SME-prägel"""

# Standard-sökningar för Google Maps när David inte gett ett eget fokus.
DEFAULT_QUERIES = [
    "tillverkande företag",
    "grossist lager",
    "industriföretag",
]

# Standard-orter att kombinera söktermerna med (svensk industritäthet).
DEFAULT_REGIONS = ["Uppsala", "Västerås", "Stockholm", "Eskilstuna", "Örebro"]


# ════════════════════════════════════════════════════════════════════════════
# Läge 1 — riktig research via Apify
# ════════════════════════════════════════════════════════════════════════════

SELECT_SYSTEM = f"""Du är en lead-research-assistent för Baris AB / Logistics Doctor (David Leifsson).
{ICP_BLOCK}

Du får en lista med VERKLIGA bolag som hämtats från Google Maps. Din uppgift är att
VÄLJA UT de som bäst matchar ICP — du får ALDRIG hitta på egna bolag, bara välja bland
de givna. Hoppa över butiker, restauranger, tjänste-/konsultbolag och bolag utan fysiskt
lager. Hellre färre och säkrare.

För varje vald bolag, returnera ett objekt:
{{
  "bolag": "exakt bolagsnamn som det stod i listan",
  "bransch": "engelskt branschnamn som på LinkedIn, t.ex. 'manufacturing', 'wholesale'",
  "titel": "rollen David bör kontakta, t.ex. 'Supply Chain Manager'",
  "region": "ort om känd",
  "motivering": "1–2 meningar: varför detta bolag sannolikt har kapitalbindning i lager"
}}

Returnera ENDAST ett JSON-objekt med nyckeln "leads" som är en lista. Inga förklaringar utanför JSON."""


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _build_queries(focus: str) -> list[str]:
    """Bygg Google Maps-sökningar. Eget fokus vinner, annars standard ICP-sökningar."""
    focus = (focus or "").strip()
    if focus:
        # Davids egna ord, t.ex. 'livsmedelstillverkare i Mälardalen'
        return [focus]
    # Kombinera ett par branschtermer med ett par orter (håll antalet lågt för krediterna)
    queries = []
    for term in DEFAULT_QUERIES[:2]:
        for region in DEFAULT_REGIONS[:2]:
            queries.append(f"{term} {region}")
    return queries


def _suggest_via_apify(n: int, existing: set[str], focus: str) -> list[dict]:
    """Hämta riktiga bolag via Apify och låt Claude välja/annotera de bästa."""
    queries = _build_queries(focus)
    companies = apify.find_companies(queries, max_places=15)

    # Filtrera bort bolag vi redan har
    fresh = [c for c in companies if c["bolag"].lower() not in existing]
    if not fresh:
        return []

    # Bygg en kompakt lista åt Claude
    listing = "\n".join(
        f"- {c['bolag']} | kategori: {c.get('kategori','')} | "
        f"ort: {c.get('ort','')} | webb: {c.get('website','')}"
        for c in fresh[:40]
    )
    by_name = {c["bolag"].lower(): c for c in fresh}

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    user_message = (
        f"Här är verkliga bolag från Google Maps. Välj de {n} bästa IHA-kandidaterna "
        f"och annotera dem. Välj ENDAST bland dessa, hitta inte på egna:\n\n{listing}\n\n"
        f"Returnera JSON med nyckeln 'leads'."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1800,
        system=SELECT_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    data = _parse_json(response.content[0].text)
    raw_leads = data.get("leads", []) if isinstance(data, dict) else []

    records = []
    seen = set(existing)
    for lead in raw_leads:
        bolag = str(lead.get("bolag", "")).strip()
        if not bolag or bolag.lower() in seen:
            continue
        # Bolaget MÅSTE finnas i den verkliga listan (annars hallucinerat → skippa)
        source = by_name.get(bolag.lower())
        if not source:
            continue
        seen.add(bolag.lower())
        titel = str(lead.get("titel", "")).strip()
        bransch = str(lead.get("bransch", "")).strip()
        records.append({
            "namn": "",
            "titel": titel,
            "bolag": bolag,
            "bransch": bransch,
            "linkedin_url": "",
            "website": source.get("website", ""),
            "motivering": str(lead.get("motivering", "")).strip(),
            "score": int(score_prospect(titel, bransch)),
            "status": "pending",
            "source": "apify",
        })
        if len(records) >= n:
            break

    records.sort(key=lambda r: r["score"], reverse=True)
    return records


# ════════════════════════════════════════════════════════════════════════════
# Läge 2 — AI-gissning (fallback)
# ════════════════════════════════════════════════════════════════════════════

GUESS_SYSTEM = f"""Du är en lead-research-assistent för Baris AB / Logistics Doctor (David Leifsson).
{ICP_BLOCK}

Du ska föreslå riktiga, namngivna svenska bolag som troligen passar — inte påhittade.
Om du är osäker på ett bolag, välj ett du är mer säker på. Hellre färre och säkrare.

Returnera ENDAST ett JSON-objekt med nyckeln "leads" som är en lista. Varje lead:
{{
  "bolag": "Bolagsnamn AB",
  "bransch": "engelskt branschnamn som på LinkedIn, t.ex. 'manufacturing'",
  "titel": "rollen David bör kontakta, t.ex. 'Supply Chain Manager'",
  "region": "ort/region i Sverige om känt, annars ''",
  "motivering": "1–2 meningar: varför detta bolag sannolikt har kapitalbindning i lager"
}}
Inga förklaringar utanför JSON."""


def _suggest_via_guess(n: int, existing: set[str], focus: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    skip_text = ""
    if existing:
        sample = list(existing)[:60]
        skip_text = "\n\nUNDVIK dessa bolag (finns redan):\n" + ", ".join(sample)

    user_message = (
        f"Föreslå {n + 4} svenska bolag som matchar ICP ovan. "
        f"{focus}\n"
        f"Variera bransch och region. Ge konkreta, kända bolag.{skip_text}\n\n"
        f"Returnera JSON med nyckeln 'leads'."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=GUESS_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    data = _parse_json(response.content[0].text)
    raw_leads = data.get("leads", []) if isinstance(data, dict) else []

    records = []
    seen = set(existing)
    for lead in raw_leads:
        bolag = str(lead.get("bolag", "")).strip()
        if not bolag or bolag.lower() in seen:
            continue
        seen.add(bolag.lower())
        titel = str(lead.get("titel", "")).strip()
        bransch = str(lead.get("bransch", "")).strip()
        records.append({
            "namn": "",
            "titel": titel,
            "bolag": bolag,
            "bransch": bransch,
            "linkedin_url": "",
            "website": "",
            "motivering": str(lead.get("motivering", "")).strip(),
            "score": int(score_prospect(titel, bransch)),
            "status": "pending",
            "source": "ai",
        })
        if len(records) >= n:
            break

    records.sort(key=lambda r: r["score"], reverse=True)
    return records


# ════════════════════════════════════════════════════════════════════════════
# Publikt gränssnitt
# ════════════════════════════════════════════════════════════════════════════

def suggest_leads(n: int = 5, existing_companies: set[str] | None = None,
                  focus: str = "") -> list[dict]:
    """
    Föreslå n nya leads. Använder Apify-research om konfigurerat, annars AI-gissning.
    existing_companies = bolag att hoppa över (gemener).
    focus = valfri inriktning/sökning, t.ex. 'livsmedelstillverkare i Mälardalen'.
    """
    existing = existing_companies or set()

    if apify.is_configured():
        records = _suggest_via_apify(n, existing, focus)
        if records:
            return records
        # Apify gav inget (t.ex. tomt sökresultat) → falla tillbaka på gissning
    return _suggest_via_guess(n, existing, focus)
