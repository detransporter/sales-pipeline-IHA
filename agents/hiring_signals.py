"""
Rekryteringssignal — flaggar bolag som just nu söker inköps-/lager-/
logistikroller.

Idé (Davids egen observation från en bolagshemsida): rekryterar bolaget en
inköpschef/logistikchef/lagerchef just nu betyder det antingen att de saknar
kompetens där — perfekt tajming för en extern lagerhälsoanalys — eller att de
växer och moderniserar sina processer, också bra tajming. En stark, konkret
köpsignal att öppna samtalet med.

Ren sökning, ingen AI-tolkning — håller kostnaden nere (bara en Apify-sökning
per kontroll, inga Anthropic-tokens). Körs bara på knapptryck (views/lead_card.py),
aldrig automatiskt i bulk-bearbetningen, så krediter bara dras på bolag du
faktiskt överväger.
"""

from integrations import apify_research as apify

# Roller som ingår i själva Google-sökningen — hålls kort med de vanligaste
# chefstitlarna så frågan inte blir för bred/brusig för Google.
_QUERY_ROLES = ("inköpschef", "logistikchef", "lagerchef", "supply chain")

# Bredare lista använd för att MÄRKA vilken roll en redan hittad träff gäller
# (Google kan råka matcha fler roller än de som stod i själva frågan).
_ROLES = _QUERY_ROLES + (
    "inköpare", "lageransvarig", "logistikansvarig",
    "materialplanerare", "lagermedarbetare",
)

# Ord som visar att träffen faktiskt är en jobbannons, inte t.ex. en
# nyhetsartikel som råkar nämna en av rollerna.
_JOB_HINTS = (
    "jobb", "lediga tjänster", "ledig tjänst", "rekryterar", "söker vi",
    "vi söker", "annons", "career", "careers", "jobs", "vacancies",
    "apply", "ansök",
)

# Kända jobbsajter — en träff härifrån räknas alltid som en jobbannons även
# om ingen av _JOB_HINTS råkar finnas i den korta beskrivningen Google visar.
_JOB_DOMAINS = (
    "arbetsformedlingen.se", "indeed.com", "linkedin.com/jobs", "monster.se",
    "thehub.io", "careerbuilder", "jobbsafari", "blocket.se/jobb",
    "metrojobb.se", "academicwork.se", "manpower.se", "randstad.se",
    "workfinder.se", "cv.se",
)


def find_hiring_signals(bolag: str, max_results: int = 8) -> dict:
    """
    Sök efter jobbannonser hos bolaget för inköps-/lager-/logistikroller.

    Returnerar:
      {"hittat": bool, "roller_matchade": [...], "traffar": [{"title","url","description"}]}
    'traffar' är max 5 träffar, redan filtrerade på sådant som ser ut som en
    riktig jobbannons (inte en nyhetsartikel som råkar nämna rollen).
    Tom/negativ dict om bolagsnamn saknas eller Apify inte är konfigurerat.
    """
    bolag = (bolag or "").strip()
    if not bolag or not apify.is_configured():
        return {"hittat": False, "roller_matchade": [], "traffar": []}

    role_query = " OR ".join(f'"{r}"' for r in _QUERY_ROLES)
    query = f'"{bolag}" ({role_query}) jobb'
    hits = apify.google_search(query, max_results=max_results)

    matched: list[dict] = []
    roles_seen: set[str] = set()
    for h in hits:
        text = f"{h.get('title','')} {h.get('description','')}".lower()
        url = (h.get("url") or "").lower()
        looks_like_job = (any(j in text for j in _JOB_HINTS)
                          or any(d in url for d in _JOB_DOMAINS))
        if not looks_like_job:
            continue
        for r in _ROLES:
            if r in text:
                roles_seen.add(r)
        matched.append(h)

    return {
        "hittat": bool(matched),
        "roller_matchade": sorted(roles_seen),
        "traffar": matched[:5],
    }
