"""
Kontroll av mottagaradresser INNAN mejlet skickas.

Bakgrund (2026-08-05): sex utskick studsade samma vecka. En av dem gick till
`mn@metalcolor.com` — en domän som inte existerar. Bolagets riktiga domän,
`metalcolour.com`, låg redan sparad i samma kontaktpost; domängissningen hade
tappat ett `u`. Mejlet skickades ändå, och studsen kom först i efterhand.

Studsar är inte gratis. Både Microsoft 365 och avsändaren (One.com) mäter hur
stor andel av utskicken som studsar, och en hög andel gör att även KORREKTA
mejl börjar sorteras som skräppost. Med ~25 utskick om dagen hinner ett
systematiskt fel skada avsändarryktet innan det upptäcks manuellt.

Den här modulen svarar på en enda fråga: *kan domänen över huvud taget ta emot
mejl?* Enligt RFC 5321 levereras post till domänens MX-poster; saknas MX helt
faller leveransen tillbaka på A/AAAA. Saknas BÅDA kan adressen aldrig nås —
det är den slutsatsen vi vågar dra, och den hade fångat metalcolor.com.

Vad den medvetet INTE gör:
  - Den säger inget om att en enskild brevlåda finns. `anders.larsson@prinoth.com`
    studsade med "User Unknown" fast prinoth.com har MX. Det går bara att
    avgöra genom att fråga mottagarens server (SMTP-verifiering), vilket många
    servrar ljuger om och som riskerar att få oss blockerade. Vi gissar inte.
  - Den blockerar ALDRIG vid nätverksfel. Går uppslagningen inte att göra
    returneras None ("vet ej") och utskicket släpps igenom. En trasig
    DNS-uppkoppling ska inte hindra David från att jobba — spärren finns för
    att fånga säkra fel, inte för att vara en extra felkälla.
"""

import re
from functools import lru_cache

_ADDR_RE = re.compile(r"^[^@\s,;:<>\"]+@[^@\s,;:<>\"]+\.[a-zA-Z]{2,}$")


@lru_cache(maxsize=1024)
def domain_can_receive_mail(domain: str) -> bool | None:
    """
    True  = domänen har MX (eller A/AAAA att falla tillbaka på)
    False = varken MX eller A/AAAA — mejl kan aldrig levereras
    None  = gick inte att avgöra (DNS-fel, timeout, dnspython saknas)

    Cachad per domän: samma bolag mejlas ofta flera gånger, och svaret ändras
    inte inom en körning.
    """
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain:
        return None
    try:
        import dns.resolver
    except Exception:
        return None

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5.0   # hela uppslagningen, inte per försök
    resolver.timeout = 3.0

    import dns.resolver as _r
    saknas = (_r.NoAnswer, _r.NXDOMAIN, _r.NoNameservers)

    try:
        if resolver.resolve(domain, "MX"):
            return True
    except saknas:
        pass                  # inget MX — kolla A/AAAA nedan innan vi dömer ut
    except Exception:
        return None           # timeout eller annat nätverksfel: vet ej

    for typ in ("A", "AAAA"):
        try:
            if resolver.resolve(domain, typ):
                return True
        except saknas:
            continue
        except Exception:
            return None
    return False


def check_address(addr: str) -> tuple[bool, str]:
    """
    (ok, orsak). ok=False bara när adressen bevisligen inte går att nå —
    aldrig på en gissning och aldrig på ett nätverksfel.
    """
    addr = (addr or "").strip()
    if not addr:
        return False, "Ingen mottagaradress angiven."
    if not _ADDR_RE.match(addr):
        return False, f"'{addr}' ser inte ut som en giltig e-postadress."

    domain = addr.rsplit("@", 1)[-1]
    if domain_can_receive_mail(domain) is False:
        return False, (
            f"Domänen {domain} kan inte ta emot mejl — den saknar både "
            f"MX- och A-post i DNS. Mejlet skulle studsa direkt. "
            f"Kontrollera stavningen mot bolagets hemsida."
        )
    return True, ""
