"""
People-finder — hittar rätt PERSON att kontakta på ett bolag, säkert och gratis(-ish).

Två källor i lager (vald av David):
  1. Bolagets egen hemsida (gratis, ingen LinkedIn) — team-/kontakt-/om-oss-sidor.
  2. Google → publika LinkedIn-profiler (via Apify) — fallback när hemsidan tiger.
     Skrapar GOOGLE, inte LinkedIn, och rör aldrig Davids konto → ingen kontorisk.

Claude väger ihop källorna och pekar ut EN bästa person (namn + roll + ev. LinkedIn-URL).
Den hittar aldrig på personer — saknas data returneras tomt. David verifierar alltid
profilen innan kontakt.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

from integrations import apify_research as apify

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Roller värda att kontakta för IHA (svenska + engelska sökord till Google).
DEFAULT_ROLES = [
    "inköpschef", "logistikchef", "supply chain", "operations manager",
    "lagerchef", "ekonomichef", "CFO", "VD",
]

SYSTEM = """Du hjälper David Leifsson (Logistics Doctor) att hitta RÄTT person att kontakta på ett
bolag för att sälja IHA (lager-/kapitalbindningsanalys). Du får två källor: text från bolagets
hemsida och en lista med publika LinkedIn-träffar. Peka ut EN bästa person.

PRIORITERA roller som äger lager/inköp/logistik/drift/ekonomi:
Supply Chain Manager, Inköpschef, Logistikchef, Operations Manager, Lagerchef, COO,
CFO/Ekonomichef, och i mindre bolag VD. Undvik HR, marknad, sälj, IT.

HÅRDA REGLER:
- Hitta ALDRIG på en person. Använd bara namn som faktiskt står i källorna.
- Om en LinkedIn-träff används: kopiera URL:en EXAKT, ändra den aldrig.
- Hittar du bara ett namn på hemsidan utan LinkedIn-URL: returnera namnet med tom URL.
- Hittar du ingen lämplig person alls: returnera "namn": "".
- LinkedIn-titlar ser ofta ut som "Namn Efternamn - Roll - Bolag | LinkedIn" — plocka isär det.

Returnera ENDAST JSON:
{
  "namn": "För- och efternamn, eller tom sträng",
  "titel": "personens roll",
  "linkedin_url": "exakt URL om känd, annars tom sträng",
  "kalla": "hemsida | linkedin | ingen",
  "sakerhet": "hög | medel | låg",
  "motivering": "kort: varför denna person"
}
Ingen text utanför JSON."""


def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


def find_person(bolag: str, website: str = "", target_role: str = "",
                bransch: str = "") -> dict:
    """
    Hitta bästa person att kontakta. Returnerar dict (se SYSTEM) — namn tomt om inget hittas.
    Kör hemsidan först (gratis); kompletterar med Google→LinkedIn (kostar lite krediter).
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return {"namn": "", "kalla": "ingen", "sakerhet": "låg"}

    roles = ([target_role] if target_role else []) + DEFAULT_ROLES

    # Källa 1 — hemsidan (gratis)
    website_text = ""
    if website:
        try:
            website_text = apify.fetch_people_pages(website)
        except Exception:
            website_text = ""

    # Källa 2 — Google → publika LinkedIn-profiler (fallback, kostar krediter)
    linkedin_hits = []
    try:
        linkedin_hits = apify.find_linkedin_profiles(bolag, roles, max_results=10)
    except Exception:
        linkedin_hits = []

    if not website_text and not linkedin_hits:
        return {"namn": "", "kalla": "ingen", "sakerhet": "låg",
                "motivering": "Ingen publik källa hittade en person."}

    li_block = "\n".join(
        f"- {h['title']} | {h['url']} | {h.get('description','')[:160]}"
        for h in linkedin_hits[:10]
    ) or "(inga LinkedIn-träffar)"

    web_block = website_text[:3000] if website_text else "(ingen hemsidetext)"

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    user_message = (
        f"Bolag: {bolag}\n"
        f"Bransch: {bransch}\n"
        f"Roll vi helst vill nå: {target_role or '(ospecificerad — välj lämpligast)'}\n\n"
        f"KÄLLA 1 — text från bolagets hemsida:\n{web_block}\n\n"
        f"KÄLLA 2 — publika LinkedIn-träffar (från Google):\n{li_block}\n\n"
        f"Peka ut den bästa personen att kontakta. Returnera JSON."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    data = _parse_json(response.content[0].text)

    # Skyddsnät: en påhittad LinkedIn-URL måste finnas bland träffarna
    url = str(data.get("linkedin_url", "")).strip()
    if url and url.lower() not in {h["url"].lower() for h in linkedin_hits}:
        url = ""

    namn = str(data.get("namn", "")).strip()

    # Konstruera personlig e-post om vi vet vem personen är.
    # Skickar med hemsidetext-mejl om några hittades där (förbättrar mönsterinferens).
    email_info: dict = {}
    if namn and website:
        try:
            # Extrahera mejl ur den redan hämtade hemsitetexten som snabbkälla till mönster.
            existing = apify._EMAIL_RE.findall(website_text)
            email_info = apify.construct_person_email(namn, website, existing)
        except Exception:
            email_info = {}

    return {
        "namn": namn,
        "titel": str(data.get("titel", "")).strip() or target_role,
        "linkedin_url": url,
        "kalla": str(data.get("kalla", "")).strip() or "ingen",
        "sakerhet": str(data.get("sakerhet", "")).strip() or "låg",
        "motivering": str(data.get("motivering", "")).strip(),
        "kandidater": linkedin_hits[:5],
        # E-post konstruerad från namnmönster + domän
        "email": email_info.get("email", ""),
        "email_candidates": email_info.get("candidates", []),
        "email_pattern": email_info.get("pattern", ""),
        "email_verified": email_info.get("verified"),
        "email_catch_all": email_info.get("catch_all", False),
    }
