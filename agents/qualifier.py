import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

from agents.model_config import MODEL_STANDARD as MODEL

QUALIFIER_SYSTEM = """Du är en sälj-kvalificeringsassistent för Baris AB (Logistics Doctor).
Analysera ett inkommande LinkedIn-svar och kategorisera det.

Returnera ett JSON-objekt med exakt dessa nycklar:
{
  "kategori": "INTRESSERAD|INTE_NU|INTE_RELEVANT|BOKA_MOTE",
  "nästa_steg": "kort beskrivning av vad säljaren ska göra härnäst",
  "förslag_svar": "ett kort svarsmeddelande säljaren kan använda (på svenska)"
}

Kategorier:
- INTRESSERAD: bekräftar att de har lager, verkar öppna för dialog
- INTE_NU: har lager men är inte i köpläge just nu
- INTE_RELEVANT: har inte med lager att göra, eller tydligt ointresserade
- BOKA_MOTE: frågar om mer info, föreslår ett samtal, eller ber om en träff

Inga förklaringar utanför JSON."""


_FALLBACK = {
    "kategori": "OKÄND",
    "nästa_steg": "Kunde inte analysera svaret automatiskt — läs igenom manuellt.",
    "förslag_svar": "",
}


def qualify_reply(svar_text: str) -> dict:
    """
    Analyse a LinkedIn reply and return categorisation + next step + suggested response.
    Returns dict with kategori, nästa_steg, förslag_svar. Kraschar aldrig — vid
    API-fel eller trasig JSON returneras _FALLBACK istället (samma tåliga mönster
    som övriga AI-anropande agenter i appen har, t.ex. lead_finder._parse_json).
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        response = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=QUALIFIER_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Analysera detta LinkedIn-svar:\n\n{svar_text}",
            }],
        )

        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)
        # Fyll ut ev. saknade nycklar så anropare alltid kan lita på formen.
        return {**_FALLBACK, **data}
    except Exception:
        return dict(_FALLBACK)


# Mapping from qualifier category to Supabase status
CATEGORY_TO_STATUS = {
    "INTRESSERAD": "svar_ja",
    "INTE_NU": "svar_ja",
    "INTE_RELEVANT": "svar_nej",
    "BOKA_MOTE": "mote_bokat",
}
