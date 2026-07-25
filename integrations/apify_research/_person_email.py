"""
E-postmönster-konstruktion för en NAMNGIVEN person: gissar troliga adresser
utifrån svenska namnmönster, identifierar bolagets mönster från redan hittade
adresser, och verifierar bästa kandidaten via SMTP (best-effort, inget mejl
skickas).

Del av `integrations/apify_research` — se paketets `__init__.py`.
"""

import re
import smtplib
import urllib.parse

from ._contact import _ascii_name
from ._scrape import _normalize_url


def _generate_email_variants(namn: str, domain: str) -> list[str]:
    """
    Generera troliga e-postadresser för en person på given domän.
    Täcker de vanligaste svenska namnmönstren.
    """
    parts = _ascii_name(namn).split()
    if len(parts) < 2 or not domain:
        return []
    f = parts[0].split("-")[0]   # Per-Erik → per
    e = parts[-1]
    fi = f[0] if f else ""

    raw = [
        f"{f}.{e}@{domain}",        # karin.lindqvist  (vanligast i Sverige)
        f"{fi}.{e}@{domain}",       # k.lindqvist
        f"{f}@{domain}",            # karin
        f"{fi}{e}@{domain}",        # klindqvist
        f"{f}{e}@{domain}",         # karinlindqvist
        f"{f}-{e}@{domain}",        # karin-lindqvist
        f"{e}.{f}@{domain}",        # lindqvist.karin
        f"{e}{fi}@{domain}",        # lindqvistk
    ]
    return list(dict.fromkeys(c for c in raw if len(c) > 5))


_GENERIC_LOCALS = frozenset({
    "info", "kontakt", "contact", "order", "sales", "ekonomi", "faktura",
    "noreply", "no-reply", "support", "admin", "webmaster", "postmaster",
    "press", "media", "gdpr", "privacy", "jobb", "career", "invoice",
    "hej", "hello", "service", "kundservice", "kundtjanst",
})


def _infer_pattern(emails: list[str], domain: str) -> str:
    """
    Identifiera bolagets e-postmönster från redan hittade adresser.
    Returnerar 'f.e' (fornamn.efternamn), 'fi.e' (initial.efternamn), eller ''.
    """
    for e in emails:
        local, _, dom = e.partition("@")
        if domain not in dom:
            continue
        if any(local == g or local.startswith(g) for g in _GENERIC_LOCALS):
            continue
        if "." in local:
            parts = local.split(".")
            if len(parts) == 2:
                return "fi.e" if len(parts[0]) == 1 else "f.e"
    return ""


def _mx_host(domain: str) -> str:
    """MX-host för domänen (kräver dnspython). Faller tillbaka på domänen själv."""
    try:
        import dns.resolver
        records = dns.resolver.resolve(domain, "MX", lifetime=5)
        return sorted(records, key=lambda r: r.preference)[0].exchange.to_text().rstrip(".")
    except Exception:
        return domain


def _smtp_verify(email: str) -> tuple:
    """
    SMTP RCPT TO-verifiering — skickar inget mejl.
    Returnerar (exists: bool|None, catch_all: bool).
    None = kunde inte avgöra (port 25 blockerad hos ISP, timeout etc.).
    """
    if "@" not in email:
        return None, False
    domain = email.split("@", 1)[1]
    mx = _mx_host(domain)
    try:
        smtp = smtplib.SMTP(timeout=7)
        smtp.connect(mx, 25)
        smtp.ehlo("logistics-doctor.se")
        smtp.mail("noreply@logistics-doctor.se")
        # Catch-all-koll: om en uppenbart falsk adress accepteras = catch-all
        fake = f"xprobe99xyz@{domain}"
        catch_code, _ = smtp.rcpt(fake)
        if catch_code == 250:
            smtp.quit()
            return None, True
        code, _ = smtp.rcpt(email)
        smtp.quit()
        return code == 250, False
    except Exception:
        return None, False


def construct_person_email(namn: str, website: str,
                           existing_emails: list | None = None) -> dict:
    """
    Konstruera trolig personlig e-postadress för en namngiven person.

    Strategi:
      1. Extrahera domänen från hemsidan.
      2. Identifiera bolagets namnmönster från redan hittade adresser (om sådana finns).
      3. Generera kandidater; om mönstret är känt lyfts den matchande kandidaten överst.
      4. SMTP-verifiering best-effort (fungerar från servermiljö; tyst fail på hemmanät).

    Returnerar:
      {
        "email":      str,        # bästa kandidat (tom om namn/domän saknas)
        "candidates": list[str],  # upp till 6 kandidater i prioritetsordning
        "pattern":    str,        # identifierat mönster ('f.e', 'fi.e', '')
        "verified":   bool|None,  # SMTP-svar (None = kunde inte verifiera)
        "catch_all":  bool,
      }
    """
    domain = urllib.parse.urlparse(_normalize_url(website)).netloc.lower().replace("www.", "")
    if not domain or not namn:
        return {"email": "", "candidates": [], "pattern": "", "verified": None, "catch_all": False}

    existing = existing_emails or []
    pattern = _infer_pattern(existing, domain)
    candidates = _generate_email_variants(namn, domain)

    # Lyft den kandidat som matchar det identifierade mönstret
    if pattern == "f.e":
        pref = [c for c in candidates if re.match(r"^[a-z]{2,}\.[a-z]{2,}@", c)]
    elif pattern == "fi.e":
        pref = [c for c in candidates if re.match(r"^[a-z]\.[a-z]{2,}@", c)]
    else:
        pref = []
    if pref:
        candidates = pref + [c for c in candidates if c not in pref]

    best = candidates[0] if candidates else ""
    verified, catch_all = None, False

    if best:
        verified, catch_all = _smtp_verify(best)
        # SMTP sa nej → prova nästa kandidat
        if verified is False:
            for cand in candidates[1:4]:
                v, ca = _smtp_verify(cand)
                if v is True:
                    best, verified, catch_all = cand, True, ca
                    break

    return {
        "email": best,
        "candidates": candidates[:6],
        "pattern": pattern,
        "verified": verified,
        "catch_all": catch_all,
    }
