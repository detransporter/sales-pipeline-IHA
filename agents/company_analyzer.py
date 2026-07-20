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
from agents import iha_metrics
from agents.model_config import MODEL_QUALITY as MODEL, MODEL_FAST as CLASSIFY_MODEL

load_dotenv()


def classify_business_model(bolag: str, bransch: str = "", website_text: str = "") -> str:
    """
    Klassa bolagets affärsmodell utifrån hemsidan → 'tillverkning' | 'grossist' |
    'handel' | 'bygg'. Styr vilken DOS-branschnorm analysen jämför mot (mycket mer
    pålitligt än att gissa på den tvetydiga SNI-etiketten). '' om osäkert/ingen AI.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or not (website_text or "").strip():
        return ""
    prompt = (
        f"Bolag: {bolag}\nBranschetikett: {bransch or '(okänd)'}\n\n"
        f"TEXT FRÅN HEMSIDAN:\n{website_text[:1500]}\n\n"
        "Klassa bolagets HUVUDSAKLIGA affärsmodell som EXAKT ett av dessa ord:\n"
        "- tillverkning (tillverkar/producerar egna varor)\n"
        "- grossist (distribuerar/säljer andras varor i parti, reservdelar, förnödenheter)\n"
        "- handel (detaljhandel/e-handel mot slutkund)\n"
        "- bygg (bygg/installation/entreprenad)\n"
        "Svara med BARA ordet, inget annat."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLASSIFY_MODEL, max_tokens=12,
            messages=[{"role": "user", "content": prompt}])
        word = "".join(b.text for b in resp.content if b.type == "text").strip().lower()
        word = word.split()[0].strip(".,:;") if word else ""
        return word if word in iha_metrics.BUSINESS_MODELS else ""
    except Exception:
        return ""

# Standardantaganden (Davids IHA-ramverk). Justerbara om vi vill senare.
CARRYING_COST_PCT = 0.20          # årlig lagerhållningskostnad ~20% av varulagervärdet
RELEASE_LOW_PCT = 0.15            # försiktig uppskattning av frigörbart kapital
RELEASE_HIGH_PCT = 0.25          # optimistisk uppskattning

SYSTEM = """Du är David Leifsson (Logistics Doctor / Baris AB), supply chain-konsult med 20 års
erfarenhet. Du gör en DJUP föranalys av ett bolag inför en första kontakt, för att sälja IHA
(Inventory Health Assessment — en analys som frigör kapital bundet i för stora/döda lager).

Du får bolagets FÖRBERÄKNADE nyckeltal (Days of Stock via bruttomarginal, lageromsättnings-
hastighet, flerårstrender, överlager mot branschnorm, lagerkostnad som andel av vinsten,
frigörbart kapital) plus text från hemsidan. Talen är redan uträknade ur deras egna bokslut —
använd BARA dem, räkna aldrig om och hitta ALDRIG på nya tal. Saknas data: säg det rakt ut.

Detta ska bli en analys som får bolaget att tänka "de förstår våra siffror bättre än vi själva".
Gå djupt: koppla ihop trenden (t.ex. lager växer snabbare än försäljningen) med vad de gör
enligt hemsidan, och landa i en konkret kronmässig konsekvens. Var ärlig om att detta är en
hypotes ur publika bokslut som IHA:t bekräftar på artikelnivå (ABC, dödlager, ledtider).

Ton: rak, konkret, siffror före adjektiv. Ingen säljhype. Detta är Davids interna underlag.

Returnera ENDAST ett JSON-objekt:
{
  "sammanfattning": "3–4 meningar: vad bolaget gör (från hemsidan) + den starkaste bokslutssignalen + varför kapital troligen binds",
  "diagnos": "2–3 meningar: din hypotes om VARFÖR lagret vuxit/binder kapital (parametrar ingen rört, sortiment som svällt, prognosdrift e.d.) — kopplat till deras verksamhet",
  "varfor_passar": ["3–4 punkter: konkreta, sifferbelagda signaler att bolaget passar IHA"],
  "potential": "2–3 meningar med kronbelopp: frigörbart kapital, årlig lagerkostnad och vad den motsvarar (t.ex. andel av vinsten/månaders vinst)",
  "samtalskrokar": ["3 korta, vassa öppningar David kan inleda med — var och en förankrad i en specifik siffra"],
  "riskflaggor": ["1–3 osäkerheter/invändningar (t.ex. säsong, K2-förenklad rapportering), eller [] om inga"]
}
Ingen text utanför JSON."""


def _parse_json(raw: str) -> dict:
    """Tolerant JSON-extraktion — tål att modellen lindar JSON i prosa/tankeblock."""
    raw = (raw or "").strip()
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Fallback: plocka ut från första { till sista } (prosa runtom stör inte).
    try:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            return json.loads(raw[i:j + 1])
    except Exception:
        pass
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
                    anstallda=None, lagerandel=None, vinstmarginal=None,
                    orgnr: str = "", bruttomarginal=None, history=None,
                    affarsmodell: str = "") -> dict:
    """
    Gör en DJUP IHA-föranalys av ett bolag. Returnerar:
      {
        "tal": {...KPI:er...}, "kpi": {...}, "headline": str, "insights": [..],
        "caveats": [..], "affarsmodell": str, "sammanfattning": str, "diagnos": str,
        "varfor_passar": [..], "potential": str, "samtalskrokar": [..], "riskflaggor": [..]
      }
    Hämtar flerårshistorik + bruttomarginal via orgnr (gratis) om de inte skickas in.
    Klassar affärsmodellen från hemsidan (om inte `affarsmodell` skickas in) → rätt
    DOS-branschnorm. Saknas ANTHROPIC-nyckel returneras KPI:erna + en enkel text.
    """
    # Berika ur bokslutet: historik + bruttomarginal + backfill av aktuella tal.
    if orgnr and (history is None or bruttomarginal is None):
        try:
            from integrations import allabolag as _ab
            _fin = _ab.get_financials(orgnr=orgnr)
            if _fin:
                if history is None:
                    history = _fin.get("history")
                if bruttomarginal is None:
                    bruttomarginal = _fin.get("bruttomarginal")
                if omsattning_msek is None:
                    omsattning_msek = _fin.get("omsattning_msek")
                if varulager_msek is None:
                    varulager_msek = _fin.get("varulager_msek")
                if resultat_msek is None:
                    resultat_msek = _fin.get("resultat_msek")
        except Exception:
            pass

    # Hemsidetext (gratis, publik) — gör analysen konkret + underlag för klassning.
    website_text = ""
    if website:
        try:
            website_text = apify.fetch_website_text(website, max_chars=2500)
        except Exception:
            website_text = ""

    # Affärsmodell → rätt DOS-norm. Överstyrd av anropare, annars klassad (billigt).
    if not affarsmodell:
        affarsmodell = classify_business_model(bolag, bransch, website_text)

    # Deterministisk KPI-motor — alla tal, inga gissningar.
    metrics = iha_metrics.compute(
        bolag=bolag, bransch=bransch, omsattning_msek=omsattning_msek,
        varulager_msek=varulager_msek, resultat_msek=resultat_msek,
        bruttomarginal=bruttomarginal, anstallda=anstallda, lagerandel=lagerandel,
        history=history, affarsmodell=affarsmodell)
    kpi = metrics["kpi"]
    tal = compute_potential(varulager_msek) or {k: kpi[k] for k in (
        "varulager_msek", "arlig_lagerkostnad_msek",
        "frigorbart_lag_msek", "frigorbart_hog_msek") if k in kpi}

    def _base(extra):
        return {"tal": tal, "kpi": kpi, "headline": metrics["headline"],
                "insights": metrics["insights"], "caveats": metrics["caveats"],
                "affarsmodell": kpi.get("affarsmodell", ""),
                "sammanfattning": "", "diagnos": "", "varfor_passar": [],
                "potential": "", "samtalskrokar": [], "riskflaggor": [], **extra}

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _base({"sammanfattning": "AI-analys ej tillgänglig (saknar ANTHROPIC_API_KEY). "
                      "Nyckeltalen och krokarna nedan är uträknade ur bolagets bokslut."})

    # Faktarad till modellen — förberäknade tal + rangordnade krokar.
    fakta = [f"Bolag: {bolag}"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if omsattning_msek is not None:
        fakta.append(f"Omsättning: {omsattning_msek} MSEK")
    if varulager_msek is not None:
        fakta.append(f"Varulager: {varulager_msek} MSEK")
    if resultat_msek is not None:
        fakta.append(f"Rörelseresultat: {resultat_msek} MSEK")
    if anstallda is not None:
        fakta.append(f"Anställda: {anstallda}")
    if kpi.get("bruttomarginal_pct"):
        fakta.append(f"Bruttomarginal: {kpi['bruttomarginal_pct']}%")
    if kpi.get("dos_dagar"):
        fakta.append(f"Days of Stock: ~{kpi['dos_dagar']} dagar "
                     f"({kpi.get('lageroms_hastighet','?')} lagervarv/år), branschnorm "
                     f"{kpi['dos_norm_lag']}–{kpi['dos_norm_hog']} ({kpi['dos_norm_bransch']})")
    if kpi.get("overlager_msek"):
        fakta.append(f"Överlager mot norm: ~{kpi['overlager_dagar']} dagar ≈ "
                     f"{kpi['overlager_msek']} MSEK")
    if kpi.get("arlig_lagerkostnad_msek"):
        s = f"Årlig lagerhållningskostnad (~20%): {kpi['arlig_lagerkostnad_msek']} MSEK"
        if kpi.get("lagerkostnad_andel_av_vinst_pct"):
            s += f" (~{kpi['lagerkostnad_andel_av_vinst_pct']}% av rörelseresultatet)"
        fakta.append(s)
    if kpi.get("frigorbart_lag_msek"):
        s = (f"Frigörbart kapital (15–25% av varulager): {kpi['frigorbart_lag_msek']}–"
             f"{kpi['frigorbart_hog_msek']} MSEK")
        if kpi.get("frigorbart_manader_vinst"):
            s += f" (~{kpi['frigorbart_manader_vinst']} månaders vinst)"
        fakta.append(s)

    krok_block = ""
    if metrics["insights"]:
        krok_block = ("\nRANGORDNADE KROKAR (starkast först — bygg samtalskrokarna på dessa):\n"
                      + "\n".join(f"- {t}" for t in metrics["insights"]))
    if metrics["headline"]:
        krok_block += f"\n\nHEADLINE: {metrics['headline']}"

    web_block = website_text if website_text else "(ingen hemsidetext tillgänglig)"
    user_message = (
        "Gör den djupa föranalysen. Förberäknade nyckeltal (använd bara dessa tal):\n"
        + "\n".join(f"- {f}" for f in fakta)
        + krok_block
        + f"\n\nTEXT FRÅN BOLAGETS HEMSIDA:\n{web_block}\n\nReturnera JSON."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL, max_tokens=1800, system=SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text")
        data = _parse_json(raw)
    except Exception as e:
        return _base({"sammanfattning": f"Kunde inte göra AI-analys: {e}"})

    if not data:
        # Anropet gick igenom men svaret gick inte att tolka som JSON.
        return _base({"sammanfattning": "AI-texten kunde inte tolkas den här gången — "
                      "nyckeltalen och krokarna ovan gäller ändå. Testa 'Gör om analys'."})

    return _base({
        "sammanfattning": str(data.get("sammanfattning", "")).strip(),
        "diagnos": str(data.get("diagnos", "")).strip(),
        "varfor_passar": [str(x).strip() for x in (data.get("varfor_passar") or []) if str(x).strip()],
        "potential": str(data.get("potential", "")).strip(),
        "samtalskrokar": [str(x).strip() for x in (data.get("samtalskrokar") or []) if str(x).strip()],
        "riskflaggor": [str(x).strip() for x in (data.get("riskflaggor") or []) if str(x).strip()],
    })
