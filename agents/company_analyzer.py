"""
Bolagsanalys — gör en ordentlig IHA-genomgång av ett lead innan David tar kontakt.

Kombinerar TVÅ källor:
  1. Bolagets EGNA siffror från Allabolag (omsättning, varulager, resultat, anställda)
     → deterministiska nyckeltal räknas ut i Python (ingen AI-matematik, inga gissade tal).
  2. Text från bolagets hemsida → vad de tillverkar/säljer, så analysen blir konkret.

Claude väver ihop detta till en kort, säljbar bild: varför just detta bolag binder
kapital i lager, ungefär hur mycket som kan frigöras, och tre samtalskrokar att öppna med.
Den hittar ALDRIG på siffror — den får bara använda talen nedan. Saknas data sägs det rakt ut.

Returnerar en dict (se analyze_company) som app.py renderar i Leads-vyn.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

from integrations import apify_research as apify

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Standardantaganden (Davids IHA-ramverk). Justerbara om vi vill senare.
CARRYING_COST_PCT = 0.20          # årlig lagerhållningskostnad ~20% av varulagervärdet
RELEASE_LOW_PCT = 0.15            # försiktig uppskattning av frigörbart kapital
RELEASE_HIGH_PCT = 0.25          # optimistisk uppskattning

SYSTEM = """Du är David Leifsson (Logistics Doctor / Baris AB), supply chain-konsult med 20 års
erfarenhet. Du gör en kort FÖRANALYS av ett bolag inför en första kontakt, för att sälja IHA
(Inventory Health Assessment — en analys som frigör kapital bundet i för stora/döda lager).

Du får bolagets verkliga nyckeltal (redan uträknade) och text från deras hemsida. Skriv en
skarp, konkret bedömning på svenska. Använd BARA de siffror du fått — hitta aldrig på tal.
Om hemsidetext saknas: säg det och håll dig till siffrorna.

Ton: rak, konkret, siffror före adjektiv. Ingen säljhype. Detta är Davids interna underlag.

Returnera ENDAST ett JSON-objekt:
{
  "sammanfattning": "2–3 meningar: vad bolaget gör + varför lagret troligen binder kapital",
  "varfor_passar": ["3–4 punkter: konkreta signaler att bolaget passar IHA"],
  "potential": "1–2 meningar om frigörbart kapital och årlig lagerkostnad, med SEK-belopp ur talen",
  "samtalskrokar": ["3 korta öppningar David kan inleda kontakten med, baserade på bolaget"],
  "riskflaggor": ["1–3 osäkerheter/invändningar att vara medveten om, eller [] om inga"]
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


def compute_potential(varulager_msek) -> dict:
    """Deterministiska IHA-tal ur varulagervärdet (inga gissningar). Tomt om varulager saknas."""
    try:
        v = float(varulager_msek)
    except (TypeError, ValueError):
        return {}
    if v <= 0:
        return {}
    return {
        "varulager_msek": round(v, 1),
        "arlig_lagerkostnad_msek": round(v * CARRYING_COST_PCT, 1),
        "frigorbart_lag_msek": round(v * RELEASE_LOW_PCT, 1),
        "frigorbart_hog_msek": round(v * RELEASE_HIGH_PCT, 1),
    }


def analyze_company(bolag: str, bransch: str = "", website: str = "",
                    omsattning_msek=None, varulager_msek=None, resultat_msek=None,
                    anstallda=None, lagerandel=None, vinstmarginal=None) -> dict:
    """
    Gör en IHA-föranalys av ett bolag. Returnerar:
      {
        "tal": {...deterministiska nyckeltal...},
        "sammanfattning": str, "varfor_passar": [..], "potential": str,
        "samtalskrokar": [..], "riskflaggor": [..]
      }
    Saknas ANTHROPIC-nyckel eller går något fel returneras bara 'tal' + en enkel text.
    """
    tal = compute_potential(varulager_msek)

    # Hemsidetext (gratis, publik) — gör analysen konkret om den finns.
    website_text = ""
    if website:
        try:
            website_text = apify.fetch_website_text(website, max_chars=2500)
        except Exception:
            website_text = ""

    # Faktarad till modellen — bara det vi faktiskt vet.
    fakta = [f"Bolag: {bolag}"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if omsattning_msek is not None:
        fakta.append(f"Omsättning: {omsattning_msek} MSEK")
    if varulager_msek is not None:
        fakta.append(f"Varulager: {varulager_msek} MSEK")
    if resultat_msek is not None:
        fakta.append(f"Resultat: {resultat_msek} MSEK")
    if anstallda is not None:
        fakta.append(f"Anställda: {anstallda}")
    if lagerandel is not None:
        fakta.append(f"Lagerandel (varulager/omsättning): {lagerandel}%")
    if vinstmarginal is not None:
        fakta.append(f"Vinstmarginal: {vinstmarginal}%")
    if tal:
        fakta.append(f"Uppskattad årlig lagerhållningskostnad (~20%): "
                     f"{tal['arlig_lagerkostnad_msek']} MSEK")
        fakta.append(f"Uppskattat frigörbart kapital (15–25% av varulager): "
                     f"{tal['frigorbart_lag_msek']}–{tal['frigorbart_hog_msek']} MSEK")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Ingen AI tillgänglig — ge åtminstone en faktabaserad sammanfattning.
        return {
            "tal": tal,
            "sammanfattning": "AI-analys ej tillgänglig (saknar ANTHROPIC_API_KEY). "
                              "Nyckeltalen nedan är uträknade ur bolagets bokslut.",
            "varfor_passar": [], "potential": "", "samtalskrokar": [], "riskflaggor": [],
        }

    web_block = website_text if website_text else "(ingen hemsidetext tillgänglig)"
    user_message = (
        "Gör föranalysen. Bolagets verkliga nyckeltal (använd bara dessa tal):\n"
        + "\n".join(f"- {f}" for f in fakta)
        + f"\n\nTEXT FRÅN BOLAGETS HEMSIDA:\n{web_block}\n\nReturnera JSON."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL, max_tokens=900, system=SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
        data = _parse_json(response.content[0].text)
    except Exception as e:
        return {
            "tal": tal,
            "sammanfattning": f"Kunde inte göra AI-analys: {e}",
            "varfor_passar": [], "potential": "", "samtalskrokar": [], "riskflaggor": [],
        }

    return {
        "tal": tal,
        "sammanfattning": str(data.get("sammanfattning", "")).strip(),
        "varfor_passar": [str(x).strip() for x in (data.get("varfor_passar") or []) if str(x).strip()],
        "potential": str(data.get("potential", "")).strip(),
        "samtalskrokar": [str(x).strip() for x in (data.get("samtalskrokar") or []) if str(x).strip()],
        "riskflaggor": [str(x).strip() for x in (data.get("riskflaggor") or []) if str(x).strip()],
    }
