"""
LinkedIn-inkorg — READ ONLY.

Hämtar inkommande LinkedIn-meddelanden via Unipile så att inkorg-agenten kan
upptäcka vem som svarat. Den SKICKAR ALDRIG något — bara läser. Det är medvetet:
lägst risk för kontot och David vill svara själv.

Källan är utbytbar: allt annat i appen pratar bara med fetch_recent_messages(),
så om du byter leverantör behöver bara den här filen ändras.

Konfiguration i .env:
    UNIPILE_DSN=apiXXX.unipile.com:13XXX     (din Unipile-DSN, utan https://)
    UNIPILE_API_KEY=...                       (X-API-KEY)
    UNIPILE_ACCOUNT_ID=...                     (LinkedIn-kontots id i Unipile)

Fältnamn normaliseras tåligt eftersom API-svar kan variera lätt.
"""

import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DSN = os.getenv("UNIPILE_DSN", "").replace("https://", "").replace("http://", "").rstrip("/")
API_KEY = os.getenv("UNIPILE_API_KEY", "")
ACCOUNT_ID = os.getenv("UNIPILE_ACCOUNT_ID", "")

TIMEOUT = 20


def is_configured() -> bool:
    return bool(DSN and API_KEY and ACCOUNT_ID)


def _base_url() -> str:
    return f"https://{DSN}/api/v1"


def _headers() -> dict:
    return {"X-API-KEY": API_KEY, "Accept": "application/json"}


def _first(d: dict, *keys, default=""):
    """Returnera första nyckeln som finns och inte är None."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _normalize(msg: dict) -> dict:
    """Plocka ut ett gemensamt format ur ett Unipile-meddelande, tåligt mot fältvariationer."""
    sender = msg.get("sender") or msg.get("from") or {}
    if isinstance(sender, str):
        sender = {"name": sender}

    sender_name = _first(msg, "sender_name", "from_name") or _first(sender, "name", "display_name", "attendee_name")
    sender_url = _first(msg, "sender_profile_url") or _first(
        sender, "profile_url", "public_profile_url", "linkedin_url", "url"
    )

    text = _first(msg, "text", "body", "message", "content")

    # Är meddelandet från mig (utgående)? Olika API:er flaggar olika.
    is_from_me = bool(
        msg.get("is_sender") is True
        or msg.get("from_me") is True
        or str(msg.get("direction", "")).lower() in ("out", "outbound", "sent")
    )

    received_at = _first(msg, "timestamp", "date", "created_at", "received_at")

    return {
        "external_id": str(_first(msg, "id", "message_id", "provider_id", default="")),
        "chat_id": str(_first(msg, "chat_id", "conversation_id", default="")),
        "sender_name": (sender_name or "").strip(),
        "sender_url": (sender_url or "").strip(),
        "text": (text or "").strip(),
        "received_at": received_at or datetime.now(timezone.utc).isoformat(),
        "is_from_me": is_from_me,
    }


def list_accounts() -> list[dict]:
    """
    Lista anslutna konton i Unipile — används vid uppsättning för att hitta
    ditt LinkedIn-konto-id (UNIPILE_ACCOUNT_ID). Kräver bara DSN + API_KEY.
    """
    if not (DSN and API_KEY):
        return []
    try:
        r = requests.get(f"{_base_url()}/accounts", headers=_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for a in (items or []):
        if not isinstance(a, dict):
            continue
        out.append({
            "id": _first(a, "id", "account_id", "object_id"),
            "name": _first(a, "name", "username", "display_name"),
            "type": _first(a, "type", "provider", "source"),
        })
    return out


def fetch_recent_messages(limit: int = 50) -> list[dict]:
    """
    Hämta de senaste meddelandena för det kopplade LinkedIn-kontot.
    Returnerar normaliserade dicts (se _normalize). Endast läsning.
    Tom lista om ej konfigurerat eller vid fel (tål fel — kraschar inte agenten).
    """
    if not is_configured():
        return []
    try:
        r = requests.get(
            f"{_base_url()}/messages",
            headers=_headers(),
            params={"account_id": ACCOUNT_ID, "limit": limit},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    # Unipile returnerar oftast {"items": [...]} men kan variera
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    return [_normalize(m) for m in items if isinstance(m, dict)]


def fetch_inbound_replies(limit: int = 50) -> list[dict]:
    """Bara inkommande meddelanden (från andra, inte från David)."""
    return [m for m in fetch_recent_messages(limit=limit)
            if not m["is_from_me"] and m["text"]]
