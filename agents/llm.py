"""
Gemensamt för alla agenter som pratar med Claude: klienten och JSON-tolkningen.

Skapad 2026-08-03. Innan detta fanns:
  - `anthropic.Anthropic(api_key=os.getenv(...))` inskrivet på 12 ställen i 8
    filer, med olika inställningar (vissa max_retries=5, vissa timeout=30, de
    flesta inget) utan att någon fil förklarade varför de skilde sig.
  - `_parse_json()` kopierad i FYRA agenter i tre olika versioner. Den i
    company_analyzer.py var mest robust (tålde None och plockade ut JSON ur
    omgivande prosa); de i conversation.py och lead_finder.py gav upp direkt
    och returnerade {} så fort modellen råkade lägga ett ord runt JSON:en.
    lead_finder.py kraschade dessutom på None.

Den robusta varianten är den som ligger här, så alla agenter fick den.
"""

import json
import os

import anthropic


def client(**kwargs) -> anthropic.Anthropic:
    """
    Anthropic-klient med nyckeln från miljön.

    kwargs skickas rakt igenom, så anropare med särskilda behov behåller dem:
      max_retries=5  — email_writer (mejlutkast är dyra att göra om manuellt)
      timeout=30.0   — people_finder (bulk-körning får inte hänga på ett bolag)
    """
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), **kwargs)


def parse_json(raw: str) -> dict:
    """
    Tolerant JSON-extraktion — tål att modellen lindar JSON i prosa, i
    ```-block, eller i ett tankeblock. Returnerar {} när inget går att tolka
    (aldrig ett undantag — anroparen får hantera tomt resultat).
    """
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
