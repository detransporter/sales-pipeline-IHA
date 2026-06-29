"""
Eget minne — agenternas berättande långtidsminne, helt i din egen Supabase.

Detta är en drop-in-ersättare för Open Brain (samma funktioner: capture_thought,
search_thoughts, list_thoughts, is_configured). Inget externt beroende — allt
ligger i tabellen `agent_memory` i din Supabase. Tål fel: om något går snett
returneras tomt/False istället för att krascha orchestratorn.
"""

import os
from dotenv import load_dotenv

from database import supabase_client as db

load_dotenv()


def capture_thought(content: str, tags: str | None = None) -> bool:
    """Spara en notering till minnet. Returnerar True vid lyckat."""
    try:
        row = db.insert_memory(content, tags=tags)
        return bool(row)
    except Exception:
        return False


def list_thoughts(limit: int = 15) -> str:
    """De senaste noteringarna som sammanfogad text."""
    try:
        rows = db.list_memory(limit=limit)
    except Exception:
        return ""
    return "\n\n".join(
        f"[{(r.get('created_at') or '')[:10]}] {r.get('content', '')}" for r in rows
    )[:3000]


def search_thoughts(query: str) -> str:
    """Sök i minnet; faller tillbaka på de senaste noteringarna om inget matchar."""
    try:
        rows = db.search_memory(query)
    except Exception:
        rows = []
    if not rows:
        return list_thoughts(limit=15)
    return "\n\n".join(
        f"[{(r.get('created_at') or '')[:10]}] {r.get('content', '')}" for r in rows
    )[:3000]


def is_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"))
