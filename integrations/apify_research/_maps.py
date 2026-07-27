"""
Apify-actorer: låg nivå (kör en actor, tolka svaret) + Google Maps-bolagssök
och Google-sökning (används av e-postsök och people_finder för publika
LinkedIn-profiler). Allt som pratar direkt med Apifys API bor här.

Del av `integrations/apify_research` — se paketets `__init__.py` för varför
den gamla enfilslösningen delades upp.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
MAPS_ACTOR = os.getenv("APIFY_MAPS_ACTOR", "compass/crawler-google-places").strip()
GOOGLE_ACTOR = os.getenv("APIFY_GOOGLE_ACTOR", "apify/google-search-scraper").strip()
# Renderande crawler (kör JS) — fallback för Wix/React-sajter där e-posten inte finns
# i rå HTML. Dyrare än plain scrape, så den körs BARA när vanlig skrapning gett noll.
RENDER_ACTOR = os.getenv("APIFY_RENDER_ACTOR", "apify/website-content-crawler").strip()

# run-sync väntar tills körningen är klar. 5 min var ohållbart i people_finder
# (David satt och väntade på en enda person) — 90 sek räcker för 3 sidor och
# ger upp snabbt istället för att hänga.
RUN_TIMEOUT = 90

# Senaste Apify-felet i klartext (tomt = inget fel). Sätts av _run_actor så att
# UI:t kan säga t.ex. "krediterna slut" istället för att tyst hitta ingenting.
#
# VIKTIGT: läs/nollställ ALLTID via get_last_error()/clear_last_error() nedan,
# aldrig genom att importera LAST_APIFY_ERROR som ett namn någon annanstans
# (t.ex. `from ._maps import LAST_APIFY_ERROR` eller via paketets __init__.py).
# En sådan import kopierar bara STRÄNGVÄRDET vid importtillfället — när
# _run_actor senare sätter ett nytt fel uppdateras bara denna moduls egen
# variabel, aldrig kopian någon annanstans. Funktionerna nedan slår istället
# alltid upp det aktuella värdet, varje gång de anropas.
LAST_APIFY_ERROR = ""


def get_last_error() -> str:
    """Senaste Apify-felet i klartext (tomt = inget fel sen senaste kontroll)."""
    return LAST_APIFY_ERROR


def clear_last_error() -> None:
    """
    Nollställ felflaggan INNAN en ny Apify-kontroll — annars kan ett gammalt
    fel från en helt annan sökning tidigare i sessionen råka visas som om det
    gällde den nya kontrollen.
    """
    global LAST_APIFY_ERROR
    LAST_APIFY_ERROR = ""


def is_configured() -> bool:
    return bool(APIFY_TOKEN)


def remaining_usage_usd() -> float | None:
    """
    Återstående Apify-krediter i USD denna faktureringscykel (gratisplan = $5/mån),
    eller None om det inte går att läsa. Låter UI:t varna INNAN en sökning startar.
    """
    if not is_configured():
        return None
    try:
        r = requests.get(
            f"https://api.apify.com/v2/users/me/limits?token={APIFY_TOKEN}", timeout=20)
        if r.status_code != 200:
            return None
        d = r.json().get("data", {})
        limit = (d.get("limits") or {}).get("maxMonthlyUsageUsd")
        used = (d.get("current") or {}).get("monthlyUsageUsd")
        if limit is not None and used is not None:
            return round(float(limit) - float(used), 2)
    except Exception:
        return None
    return None


def _actor_path(actor: str) -> str:
    # Apify vill ha 'user~actor' i URL:en, men folk skriver 'user/actor'
    return actor.replace("/", "~")


def _run_actor(actor: str, run_input: dict) -> list[dict]:
    """
    Kör en Apify-actor synkront och returnera dataset-raderna.
    Tom lista vid fel — kraschar aldrig anropande agent.
    """
    global LAST_APIFY_ERROR
    if not is_configured():
        return []
    url = (
        f"https://api.apify.com/v2/acts/{_actor_path(actor)}"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    )
    try:
        r = requests.post(url, json=run_input, timeout=RUN_TIMEOUT)
        if r.status_code not in (200, 201):
            # Gör vanligaste felet begripligt (krediter slut) — annars generiskt.
            if r.status_code == 402:
                LAST_APIFY_ERROR = ("Apify-krediterna är slut — hemsides- och "
                                    "personsökning kräver den betalda Google-aktorn. "
                                    "Fyll på krediter på console.apify.com/billing.")
            else:
                LAST_APIFY_ERROR = f"Apify svarade {r.status_code} för aktorn {actor}."
            return []
        data = r.json()
        LAST_APIFY_ERROR = ""
    except Exception as e:
        LAST_APIFY_ERROR = f"Kunde inte nå Apify: {e}"
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [d for d in data["items"] if isinstance(d, dict)]
    return []


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _normalize_place(p: dict) -> dict:
    """Plocka ut ett gemensamt format ur ett Google Maps-resultat."""
    website = str(_first(p, "website", "url", "webUrl", default="")).strip()
    # Hoppa över rena Google/Facebook-länkar — vi vill ha bolagets egen sajt
    if any(bad in website.lower() for bad in ("google.com", "facebook.com", "instagram.com")):
        website = ""
    return {
        "bolag": str(_first(p, "title", "name", default="")).strip(),
        "website": website,
        "ort": str(_first(p, "city", "neighborhood", default="")).strip(),
        "adress": str(_first(p, "address", "street", default="")).strip(),
        "kategori": str(_first(p, "categoryName", "category", default="")).strip(),
        "telefon": str(_first(p, "phone", "phoneUnformatted", default="")).strip(),
    }


def find_companies(queries: list[str], max_places: int = 15,
                   country: str = "se", language: str = "sv") -> list[dict]:
    """
    Sök riktiga bolag via Google Maps. queries = lista av söksträngar, t.ex.
    ['tillverkare Uppsala', 'grossist Västerås']. Returnerar normaliserade
    dicts (se _normalize_place). Dubbletter (samma bolagsnamn) tas bort.
    """
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries or not is_configured():
        return []

    run_input = {
        "searchStringsArray": queries,
        "maxCrawledPlacesPerSearch": int(max_places),
        "language": language,
        "countryCode": country,
        "skipClosedPlaces": True,
    }
    raw = _run_actor(MAPS_ACTOR, run_input)

    out: list[dict] = []
    seen: set[str] = set()
    for p in raw:
        place = _normalize_place(p)
        name = place["bolag"]
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(place)
    return out


def google_search(query: str, max_results: int = 10,
                  country: str = "se", language: str = "sv") -> list[dict]:
    """
    Kör en Google-sökning via Apify och returnera organiska träffar:
    [{"title","url","description"}]. Skrapar GOOGLE, inte LinkedIn, och använder
    aldrig ditt konto. Tom lista vid fel/ej konfigurerat.
    """
    query = (query or "").strip()
    if not query or not is_configured():
        return []
    run_input = {
        "queries": query,
        "maxPagesPerQuery": 1,
        "resultsPerPage": int(max_results),
        "countryCode": country,
        "languageCode": language,
    }
    pages = _run_actor(GOOGLE_ACTOR, run_input)
    out: list[dict] = []
    for page in pages:
        for res in (page.get("organicResults") or []):
            if not isinstance(res, dict):
                continue
            out.append({
                "title": str(_first(res, "title", default="")).strip(),
                "url": str(_first(res, "url", "link", default="")).strip(),
                "description": str(_first(res, "description", "snippet", default="")).strip(),
            })
    return out


def find_linkedin_profiles(bolag: str, roles: list[str],
                           max_results: int = 10) -> list[dict]:
    """
    Hitta publika LinkedIn-profil-URL:er för rätt roll på ett bolag, via Google.
    roles = lista med roller/sökord, t.ex. ['inköpschef', 'logistikchef', 'supply chain'].
    Returnerar bara träffar på linkedin.com/in/.
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return []
    role_part = " OR ".join(f'"{r}"' for r in roles if r) or ""
    query = f'"{bolag}" {role_part} site:linkedin.com/in'.strip()
    results = google_search(query, max_results=max_results)
    return [r for r in results if "linkedin.com/in/" in r["url"].lower()]
