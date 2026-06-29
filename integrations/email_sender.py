"""
E-postsändare — skickar mejl via SMTP (t.ex. Gmail) direkt från appen.

David granskar alltid utkastet innan han trycker skicka — appen skickar aldrig
i bakgrunden. Helt lokalt: ingen tredjepartstjänst, bara ditt eget mejlkonto.

Konfiguration i .env:
    SMTP_HOST=smtp.gmail.com           (standard för Gmail)
    SMTP_PORT=587                      (STARTTLS)
    SMTP_USER=din.adress@gmail.com     (din avsändaradress)
    SMTP_PASS=xxxxxxxxxxxxxxxx         (Gmail APP-LÖSENORD, 16 tecken — INTE ditt vanliga
                                        lösenord. Skapa på myaccount.google.com → Säkerhet →
                                        Tvåstegsverifiering → App-lösenord)
    SMTP_FROM_NAME=David Leifsson      (valfritt visningsnamn)

Saknas konfiguration returnerar is_configured() False och UI:t visar instruktioner.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

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
    """True om avsändaradress + lösenord finns i .env."""
    c = _conf()
    return bool(c["user"] and c["password"])


def from_address() -> str:
    return _conf()["user"]


def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """
    Skicka ett mejl. Returnerar (ok, felmeddelande). ok=False med text vid problem.
    """
    to_addr = (to_addr or "").strip()
    if not to_addr or "@" not in to_addr:
        return False, "Ogiltig mottagaradress."
    if not (subject or "").strip() and not (body or "").strip():
        return False, "Tomt mejl — skriv ämne och text först."

    c = _conf()
    if not is_configured():
        return False, "SMTP är inte konfigurerat (lägg SMTP_USER + SMTP_PASS i .env)."

    msg = EmailMessage()
    sender = f"{c['from_name']} <{c['user']}>" if c["from_name"] else c["user"]
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject or "(utan ämne)"
    msg.set_content(body or "")

    try:
        # Använd certifi:s rot-certifikat — annars failar TLS på macOS-Python som
        # saknar systemcertifikat ("CERTIFICATE_VERIFY_FAILED").
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            context = ssl.create_default_context()
        if c["port"] == 465:
            # Implicit SSL (One.com m.fl.) — anslut direkt över TLS, ingen starttls.
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20, context=context) as server:
                server.login(c["user"], c["password"])
                server.send_message(msg)
        else:
            # STARTTLS (587, t.ex. Gmail och One.com:587).
            with smtplib.SMTP(c["host"], c["port"], timeout=20) as server:
                server.starttls(context=context)
                server.login(c["user"], c["password"])
                server.send_message(msg)
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, ("Inloggning nekades. Använd ett Gmail APP-lösenord (16 tecken), "
                       "inte ditt vanliga lösenord, och kontrollera SMTP_USER.")
    except Exception as e:
        return False, f"Kunde inte skicka: {e}"
