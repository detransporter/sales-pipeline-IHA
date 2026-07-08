"""
E-postsändare — skickar mejl via SMTP (t.ex. One.com) direkt från appen.

Använder MIMEText + sendmail() (inte EmailMessage + send_message) för att undvika
att Python kräver SMTPUTF8-stöd när ämne/text innehåller svenska tecken. Ämnen
med non-ASCII encodas via RFC 2047 (base64) vilket alla servrar förstår.

Konfiguration i .env:
    SMTP_HOST=send.one.com
    SMTP_PORT=587                      (STARTTLS)
    SMTP_USER=din.adress@barisab.com
    SMTP_PASS=dittlösenord
    SMTP_FROM_NAME=David Leifsson      (valfritt visningsnamn)
"""

import os
import smtplib
import ssl
import email.header
from email.mime.text import MIMEText
from email.utils import formataddr

from dotenv import load_dotenv

load_dotenv()


def _conf() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.getenv("SMTP_PORT", "587").strip() or 587),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASS", "").strip(),
        "from_name": os.getenv("SMTP_FROM_NAME", "").strip(),
    }


def is_configured() -> bool:
    c = _conf()
    return bool(c["user"] and c["password"])


def from_address() -> str:
    return _conf()["user"]


def _encode_header(value: str) -> str:
    """Encoda ett header-värde med RFC 2047 om det innehåller non-ASCII."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return email.header.Header(value, "utf-8").encode()


def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """
    Skicka ett mejl. Returnerar (ok, felmeddelande).
    Använder MIMEText+sendmail för att undvika SMTPUTF8-kravet på One.com.
    """
    to_addr = (to_addr or "").strip()
    if not to_addr or "@" not in to_addr:
        return False, "Ogiltig mottagaradress."
    if not (subject or "").strip() and not (body or "").strip():
        return False, "Tomt mejl — skriv ämne och text först."

    c = _conf()
    if not is_configured():
        return False, "SMTP är inte konfigurerat (lägg SMTP_USER + SMTP_PASS i .env)."

    msg = MIMEText(body or "", "plain", "utf-8")
    sender = formataddr((c["from_name"], c["user"])) if c["from_name"] else c["user"]
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = _encode_header(subject or "(utan ämne)")

    try:
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            context = ssl.create_default_context()

        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20, context=context) as server:
                server.login(c["user"], c["password"])
                server.sendmail(c["user"], to_addr, msg.as_string())
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=20) as server:
                server.starttls(context=context)
                server.login(c["user"], c["password"])
                server.sendmail(c["user"], to_addr, msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, ("Inloggning nekades. Kontrollera SMTP_USER och SMTP_PASS i .env.")
    except Exception as e:
        return False, f"Kunde inte skicka: {e}"
