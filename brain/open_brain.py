"""
Open Brain-klient — det berättande långtidsminnet för agenterna.

Open Brain är Davids personliga minne (tankar, kontaktnoter, pipeline-historik).
Här pratar vi med det via JSON-RPC. Funktionerna är medvetet tåliga: om
OPEN_BRAIN_URL/KEY saknas eller något går fel returnerar de tomt/False istället
för att krascha orchestratorn — Supabase är alltid den strukturerade sanningen,
Open Brain är minnet ovanpå.

Porterad från Sales.py så att hela systemet delar ett minne.
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

OPEN_BRAIN_URL = os.getenv("OPEN_BRAIN_URL")
OPEN_BRAIN_KEY = os.getenv("OPEN_BRAIN_KEY")


def _brain_post(payload: dict) -> str:
    """Skickar ett JSON-RPC-anrop till Open Brain och returnerar text (tom vid fel)."""
    if not OPEN_BRAIN_URL or not OPEN_BRAIN_KEY:
        return ""
    try:
        r = requests.post(
            OPEN_BRAIN_URL,
            json=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "x-brain-key": OPEN_BRAIN_KEY,
            },
            timeout=30,
        )
        r.encoding = "utf-8"
        for line in r.text.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                result = data.get("result", {})
                content = result.get("content", [])
                if content:
                    return content[0].get("text", "")
    except Exception:
        return ""
    return ""


def capture_thought(content: str) -> bool:
    """Spara en tanke/händelse till Open Brain (minnet). Returnerar True vid lyckat."""
    result = _brain_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "capture_thought", "arguments": {"content": content}},
    })
    return bool(result)


def list_thoughts(limit: int = 15) -> str:
    """Hämta de senaste tankarna (rå text, max ~3000 tecken)."""
    raw = _brain_post({
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {"name": "list_thoughts", "arguments": {"limit": limit}},
    })
    return raw[:3000] if raw else ""


def search_thoughts(query: str) -> str:
    """Sök i minnet; faller tillbaka på de senaste tankarna om sökningen är tom."""
    try:
        result = _brain_post({
            "jsonrpc": "2.0", "id": 2,
            "method": "tools/call",
            "params": {"name": "search_thoughts", "arguments": {"query": query}},
        })
    except Exception:
        result = ""
    if result and "no thoughts found" not in result.lower():
        return result
    return list_thoughts(limit=20)


def is_configured() -> bool:
    return bool(OPEN_BRAIN_URL and OPEN_BRAIN_KEY)
