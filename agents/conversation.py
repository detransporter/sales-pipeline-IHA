"""
Samtalsmotor — flyttar ett LinkedIn-samtal framåt mot ett sålt IHA, ett steg i taget.

Det här är skillnaden mellan en trevlig chatt och en bokad affär. Istället för att
bara skriva ett snällt svar läser motorn HELA samtalet, avgör var i säljtrappan ni är
och skriver nästa meddelande som flyttar personen exakt ETT steg uppåt — aldrig mer.

SÄLJTRAPPAN (värdestegen mot IHA):
  ny         → de har precis svarat första gången
  oppning    → de har bekräftat att de jobbar med lager
  upptack    → förstå hur de hanterar lager idag + var det skaver
  insikt     → spegla igenkänning, bygg förtroende, så ett litet frö
  erbjudande → erbjud en GRATIS "Inventory Health Snapshot" (dörröppnaren)
  boka       → föreslå ett kort 15-min-samtal
  vunnen     → möte bokat (mål)
  paus       → inte nu — håll dörren öppen, vänligt
  forlorad   → inte relevant — släpp snyggt

Dörröppnaren är medvetet låg tröskel: David tittar kostnadsfritt på en lager-export
och visar hur mycket kapital som ligger i döda/överstora artiklar. Det är inte en pitch
— det är ett gratis värde som naturligt leder till ett samtal.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

from database import supabase_client as db

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Trappans ordning — används för att validera att vi inte hoppar för långt.
STAGE_ORDER = [
    "ny", "oppning", "upptack", "insikt", "erbjudande", "boka", "vunnen",
]
SIDE_STAGES = {"paus", "forlorad"}
ALL_STAGES = set(STAGE_ORDER) | SIDE_STAGES


ENGINE_SYSTEM = """Du är samtalsmotorn åt David Leifsson (Logistics Doctor). Du driver ett pågående
LinkedIn-samtal framåt mot ETT mål: att David får boka ett kort samtal, genom att först
erbjuda en gratis "Inventory Health Snapshot".

OM IHA (avslöja ALDRIG för tidigt): David hjälper tillverkare/distributörer/grossister att
se hur mycket kapital de binder i för stort eller dött lager. Dörröppnaren är en KOSTNADSFRI
snabbtitt på deras lagerdata (de skickar en export) där David visar döda artiklar och bundet
kapital. Det leder naturligt till ett 15-minuterssamtal.

SÄLJTRAPPAN — flytta personen MAX ETT steg per meddelande:
- ny → oppning: de har precis svarat. Visa äkta nyfikenhet, bekräfta att de jobbar med lager.
- oppning → upptack: ställ EN lätt fråga om hur de håller koll på artiklar idag (system/Excel/känsla).
- upptack → insikt: de berättar om sin vardag/friktion. Spegla igenkänning ("känner igen det
  från andra tillverkare"), bygg förtroende. Erbjud fortfarande inget.
- insikt → erbjudande: när förtroende finns, erbjud LÅGT OCH MJUKT den gratis snapshoten
  ("om du vill kan jag ta en snabb kostnadsfri titt på en lager-export och visa var kapitalet
  ligger — inga förpliktelser"). Pitcha inte produkten, erbjud värdet.
- erbjudande → boka: de nappar på snapshoten eller är nyfikna → föreslå ett kort samtal/15 min.
- boka → vunnen: de säger ja till samtal → bekräfta varmt och föreslå att hitta en tid.

SIDOSPÅR:
- paus: "inte nu / fel tajming" → vänligt, håll dörren öppen, pressa aldrig.
- forlorad: tydligt ointresserade / inte relevant → tacka snyggt och släpp.

GYLLENE REGLER:
- Spegla deras ton och längd. Svarar de kort, svara kort.
- Lågt tryck. Aldrig säljjargong, aldrig utropstecken, ingen länk om de inte bett om den.
- Hoppa ALDRIG förbi ett steg för att det går fortare. Tålamod vinner.
- Svenska, naturligt, mänskligt. Skriv aldrig om David i tredje person.

Returnera ENDAST ett JSON-objekt:
{
  "nuvarande_steg": "var personen är NU (ett av: ny, oppning, upptack, insikt, erbjudande, boka, vunnen, paus, forlorad)",
  "nasta_steg": "vart DETTA meddelande flyttar dem (samma lista)",
  "meddelande": "det färdiga meddelandet att skicka, ordagrant, inget annat"
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


def build_history(prospect_id: str) -> str:
    """
    Bygg en läsbar avskrift av hela samtalet ur databasen (skickade DM + inkomna svar),
    i kronologisk ordning. Ger motorn full kontext istället för bara sista meddelandet.
    """
    events: list[tuple[str, str, str]] = []  # (tidsstämpel, vem, text)

    try:
        for dm in db.get_dm_history(prospect_id):
            ts = dm.get("skickad_at") or dm.get("created_at") or ""
            txt = (dm.get("meddelande") or "").strip()
            if txt:
                events.append((ts, "David", txt))
    except Exception:
        pass

    try:
        for r in db.get_replies_for_prospect(prospect_id):
            ts = r.get("received_at") or r.get("created_at") or ""
            txt = (r.get("text") or "").strip()
            if txt:
                events.append((ts, "Kontakten", txt))
    except Exception:
        pass

    events.sort(key=lambda e: e[0] or "")
    return "\n".join(f"{who}: {txt}" for _, who, txt in events)


def next_move(namn: str, titel: str, bolag: str, deras_svar: str,
              historik: str = "", nuvarande_steg: str = "") -> dict:
    """
    Avgör nästa drag i samtalet. Returnerar:
      {"nuvarande_steg": str, "nasta_steg": str, "meddelande": str}
    deras_svar = personens senaste meddelande. historik = avskrift av tidigare turer.
    nuvarande_steg = senast kända steg (hjälper motorn men den får omvärdera).
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    fornamn = namn.strip().split()[0] if namn.strip() else namn

    context = f"Kontakt: {namn} ({fornamn}), {titel} på {bolag}.\n"
    if nuvarande_steg:
        context += f"Senast kända steg i trappan: {nuvarande_steg}\n"
    if historik.strip():
        context += f"\nHela samtalet hittills:\n{historik.strip()}\n"
    context += (
        f"\nDeras senaste meddelande:\n\"{deras_svar.strip()}\"\n\n"
        f"Avgör var de är i trappan och skriv Davids nästa meddelande — max ett steg framåt."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=ENGINE_SYSTEM,
        messages=[{"role": "user", "content": context}],
    )

    data = _parse_json(response.content[0].text)
    msg = str(data.get("meddelande", "")).strip()
    nu = str(data.get("nuvarande_steg", "")).strip().lower()
    nasta = str(data.get("nasta_steg", "")).strip().lower()

    # Skyddsnät: håll stegen inom listan
    if nu not in ALL_STAGES:
        nu = nuvarande_steg or "oppning"
    if nasta not in ALL_STAGES:
        nasta = nu

    # Skyddsnät: tillåt aldrig att hoppa mer än ett steg uppåt i huvudtrappan
    if nu in STAGE_ORDER and nasta in STAGE_ORDER:
        i, j = STAGE_ORDER.index(nu), STAGE_ORDER.index(nasta)
        if j > i + 1:
            nasta = STAGE_ORDER[i + 1]

    return {"nuvarande_steg": nu, "nasta_steg": nasta, "meddelande": msg}


def stage_label(stage: str) -> str:
    """Mänsklig etikett för UI."""
    labels = {
        "ny": "🌱 Ny kontakt",
        "oppning": "👋 Öppning",
        "upptack": "🔍 Upptäcker behov",
        "insikt": "🤝 Bygger förtroende",
        "erbjudande": "🎁 Erbjuder gratis snapshot",
        "boka": "📅 Mot bokat samtal",
        "vunnen": "✅ Möte bokat",
        "paus": "⏸ Inte nu",
        "forlorad": "🚪 Inte relevant",
    }
    return labels.get(stage, stage or "—")
