"""
Rekryteringssignal — flaggar bolag som just nu söker inköps-/lager-/
logistikroller.

Idé (Davids egen observation från en bolagshemsida): rekryterar bolaget en
inköpschef/logistikchef/lagerchef just nu betyder det antingen att de saknar
kompetens där — perfekt tajming för en extern lagerhälsoanalys — eller att de
växer och moderniserar sina processer, också bra tajming. En stark, konkret
köpsignal att öppna samtalet med.

Källa: Arbetsförmedlingens Platsbank (JobTech Devs öppna JobSearch-API) —
HELT GRATIS, ingen API-nyckel, inga Apify-krediter. David har inga Apify-
krediter och kommer inte ha det, så det här ersätter en tidigare Apify-
baserad version helt. Nästan alla svenska arbetsgivare annonserar (direkt
eller via rekryteringsbolag) på Platsbanken, så täckningen är god trots att
det inte är en generell webbsökning.

Sök by frågar bolagsnamnet som EXAKT FRAS (citattecken) — testat live och
ger bara annonser som faktiskt nämner bolaget (direkt eller via ett
rekryteringsbolag), inte bara lösa ordträffar. Rollerna filtreras sedan
fram lokalt ur varje annons rubrik+text.
"""

import requests

_API_URL = "https://jobsearch.api.jobtechdev.se/search"

# Roller vars rekrytering signalerar bristande koll på lager/inköp/logistik.
_ROLES = (
    "inköpschef", "inköpare", "logistikchef", "logistikansvarig",
    "lagerchef", "lageransvarig", "supply chain", "materialplanerare",
    "produktionsplanerare", "lagermedarbetare",
)


def find_hiring_signals(bolag: str, max_results: int = 20) -> dict:
    """
    Sök Arbetsförmedlingens platsbank efter aktiva jobbannonser hos bolaget
    för inköps-/lager-/logistikroller. Gratis öppet GET-anrop, ingen nyckel.

    Returnerar {"hittat": bool, "roller_matchade": [...], "traffar": [...]}.
    'traffar' (max 5) är {"title","url","description"}. Tom/negativ dict
    vid saknat bolagsnamn eller om API:et inte går att nå.
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return {"hittat": False, "roller_matchade": [], "traffar": []}

    try:
        r = requests.get(
            _API_URL, params={"q": f'"{bolag}"', "limit": max_results}, timeout=10)
        if r.status_code != 200:
            return {"hittat": False, "roller_matchade": [], "traffar": []}
        hits = r.json().get("hits", [])
    except Exception:
        return {"hittat": False, "roller_matchade": [], "traffar": []}

    matched: list[dict] = []
    roles_seen: set[str] = set()
    for h in hits:
        headline = h.get("headline") or ""
        desc = (h.get("description") or {}).get("text") or ""
        text = f"{headline} {desc}".lower()
        role_hits = [r_ for r_ in _ROLES if r_ in text]
        if not role_hits:
            continue
        roles_seen.update(role_hits)
        matched.append({
            "title": headline or (h.get("employer") or {}).get("name", ""),
            "url": h.get("webpage_url", ""),
            "description": desc[:200].replace("\n", " ").strip(),
        })

    return {
        "hittat": bool(matched),
        "roller_matchade": sorted(roles_seen),
        "traffar": matched[:5],
    }
