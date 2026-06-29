"""
E-postskrivare — genererar ett kort, personligt kall-mejl åt David Leifsson (Logistics
Doctor) som backup-väg in när LinkedIn inte funkar.

Till skillnad från LinkedIn-DM:et (som bara ska få ett "ja/nej"-svar och aldrig pitchar)
får ett mejl vara tydligare med varför David hör av sig — men fortfarande KORT, mänskligt
och utan säljjargong. Kroken är bolagets EGNA siffror: hur mycket kapital som sitter i lager.

Returnerar {"subject": ..., "body": ...}. Body är färdig att skicka (hälsning + signatur).
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Davids signatur — alltid med i mejlet (telefon + LinkedIn så mottagaren kan nå/kolla upp honom).
SIGNATURE = (
    "Vänliga hälsningar,\n"
    "David Leifsson\n"
    "Logistics Doctor\n"
    "Tel: 0737168367\n"
    "LinkedIn: https://www.linkedin.com/in/davidleifsson/"
)

SYSTEM_PROMPT = """Du skriver ett KORT kall-mejl på svenska åt David Leifsson (Logistics Doctor /
Baris AB). David hjälper tillverkare/distributörer att frigöra kapital som sitter fast i för
stora eller döda lager, via en lättviktig analys (IHA — Inventory Health Analysis).

Mejlet går ofta till en allmän eller ledningsadress (info@, vd@, en chef). Målet är ETT kort
svar / ett ja till ett 15-minuterssamtal — inte att stänga affären i mejlet.

TON & FORM:
- Svenska, professionellt men mänskligt. Du-tilltal. Inga utropstecken, ingen säljhype.
- KORT: 4–6 korta meningar i brödtexten. Lätt att läsa på mobil.
- Använd bolagets EGNA siffror som krok (lagerandel, kapital i lager) — konkret, inte svävande.
- Var ärlig och rak: säg kort vad David gör och varför just detta bolag kan vara intressant.
- Avsluta med en låg tröskel: en fråga om de är öppna för ett kort samtal.
- Avsluta ALLTID med EXAKT denna signatur (oförändrad, inkl. telefon och LinkedIn):
""" + SIGNATURE + """
- Skriv ALDRIG påståenden om siffror du inte fått. Använd bara de siffror som ges.

Returnera ENDAST ett JSON-objekt:
{
  "subject": "kort, konkret ämnesrad (max ~7 ord), gärna med bolagsnamn eller en siffra",
  "body": "hela mejltexten, inkl. hälsning och signatur, med radbrytningar (\\n)"
}
Ingen text utanför JSON."""


def _first_name(namn: str) -> str:
    namn = (namn or "").strip()
    return namn.split()[0] if namn else ""


def generate_email(bolag: str, namn: str = "", titel: str = "", bransch: str = "",
                   lagerandel=None, varulager_msek=None, omsattning_msek=None) -> dict:
    """
    Generera {subject, body} för ett kall-mejl. Siffrorna (om de finns) blir kroken.
    namn kan vara tomt (mejl till info@/ledning) — då används en neutral hälsning.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Bygg en faktarad med det vi faktiskt vet (inga påhittade siffror)
    fakta = [f"Bolag: {bolag}"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if omsattning_msek is not None:
        fakta.append(f"Omsättning: ca {omsattning_msek} MSEK")
    if varulager_msek is not None:
        fakta.append(f"Varulager: ca {varulager_msek} MSEK")
        try:
            arlig_kostnad = round(float(varulager_msek) * 0.20, 1)
            fakta.append(f"Uppskattad årlig lagerhållningskostnad (~20%): ca {arlig_kostnad} MSEK")
        except Exception:
            pass
    if lagerandel is not None:
        fakta.append(f"Lagerandel (varulager/omsättning): {lagerandel}%")

    hals = (f"Mottagare: {namn} ({titel})" if namn
            else "Mottagare: okänd person (allmän/ledningsadress) — använd en neutral hälsning "
                 "som 'Hej,' eller 'Hej och hej till er på {bolag},'")

    user_message = (
        f"Skriv kall-mejlet. Fakta om bolaget (använd som krok, hitta inte på mer):\n"
        + "\n".join(f"- {f}" for f in fakta)
        + f"\n\n{hals}\n\n"
        f"Poängen att få fram subtilt: bolaget binder mycket kapital i lager, och David kan "
        f"visa hur mycket som kan frigöras. Be om ett kort samtal. Returnera JSON."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=700,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    body = str(data.get("body", "")).strip()
    # Garantera att telefon + LinkedIn finns med — lägg till signaturen om modellen glömt.
    if "0737168367" not in body or "linkedin.com/in/davidleifsson" not in body.lower():
        body = body.rstrip() + "\n\n" + SIGNATURE
    return {
        "subject": str(data.get("subject", "")).strip() or f"Kapital i lager hos {bolag}?",
        "body": body,
    }
