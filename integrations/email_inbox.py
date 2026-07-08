"""
Email-inkorg — hämtar olästa svar från Gmail via IMAP.

Återanvänder SMTP_USER + SMTP_PASS från .env (samma Gmail App-lösenord).
Kopplar mot imap.gmail.com:993 (SSL) om inte IMAP_HOST/IMAP_PORT är satt.

Filtrerar automatiskt på avsändaradresser vi känner igen (folk vi mejlat).
Returnerar obehandlade svar — märker INTE om meddelandena som lästa i Gmail
(det bestämmer David själv när han klickar Behandla i appen).
"""

import email
import email.header as _hdr
import imaplib
import os
import re
from email.utils import parseaddr

from dotenv import load_dotenv

load_dotenv()


def _conf() -> dict:
    return {
        "host": os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
        "port": int(os.getenv("IMAP_PORT", "993").strip() or 993),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASS", "").strip(),
    }


def is_configured() -> bool:
    c = _conf()
    return bool(c["user"] and c["password"])


def _decode_header(value: str) -> str:
    parts = _hdr.decode_header(value or "")
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _get_plain_body(msg) -> str:
    """Extrahera text/plain ur ett email.Message. Fallback: strip HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", " ", html).strip()
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def _strip_quoted(body: str) -> str:
    """Ta bort citerad text (rader med >) och "On ... wrote:"-block."""
    lines = body.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        # Sluta vid typisk citatmarkör
        if stripped.startswith(">"):
            break
        # Sluta vid "Den ... skrev:" / "On ... wrote:" / "From: "
        if re.match(r"^(Den |On |From:|\-{3,})", stripped, re.IGNORECASE):
            break
        clean.append(line)
    return "\n".join(clean).strip()


def fetch_unread_replies(known_addresses: set[str] | None = None,
                         limit: int = 30) -> list[dict]:
    """
    Hämta olästa mejl från INBOX.

    known_addresses: om angivet filtreras på From-adress (bara folk vi mejlat).
    Returnerar lista med dicts: from_addr, from_name, subject, body (citatfri),
    full_body, date, message_id, uid.
    """
    c = _conf()
    results: list[dict] = []
    known_lower = {a.lower() for a in known_addresses} if known_addresses else None

    conn = imaplib.IMAP4_SSL(c["host"], c["port"])
    try:
        conn.login(c["user"], c["password"])
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        raw_uids = data[0].split() if data[0] else []
        uids = raw_uids[-limit:]  # senaste N

        for uid in reversed(uids):
            _, msg_data = conn.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            from_raw = msg.get("From", "")
            from_name_raw, from_addr = parseaddr(from_raw)
            from_addr = from_addr.lower()

            if known_lower is not None and from_addr not in known_lower:
                continue

            full_body = _get_plain_body(msg)
            body = _strip_quoted(full_body)

            results.append({
                "from_addr": from_addr,
                "from_name": _decode_header(from_name_raw),
                "subject": _decode_header(msg.get("Subject", "")),
                "body": body or full_body[:2000],
                "full_body": full_body[:4000],
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "uid": uid.decode(),
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return results
