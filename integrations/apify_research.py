"""
Apify-research — hittar RIKTIGA bolag (inte AI-gissningar).

Det här är "research-agentens" motor. Den kör Apifys Google Maps-scraper för att
hämta verkliga svenska bolag (tillverkare/distributörer/grossister) med namn,
hemsida, ort och kategori. Den rör ALDRIG LinkedIn och använder inte ditt konto
— därför är den helt säker för ditt LinkedIn-konto. Lead-finder-agenten låter
sedan Claude välja och annotera bland dessa verkliga bolag.

Bonus: fetch_website_text() hämtar text från ett bolags hemsida (helt publikt)
så att DM-generatorn kan skriva en personlig öppning. Också säkert.

Konfiguration i .env:
    APIFY_TOKEN=apify_api_xxx          (din gratis-token från apify.com → Settings → Integrations)
    APIFY_MAPS_ACTOR=compass/crawler-google-places   (valfritt — standard räcker)

Allt är tåligt: saknas token, eller går något fel, returneras tom lista och
lead-finder faller automatiskt tillbaka på sitt gamla AI-läge.

KOSTNAD: gratisplanen ger $5/månad (rullar inte över). Google Maps är billigt —
håll max_places lågt (10–20 per sökning) så räcker krediterna långt.
"""

import os
import re
import smtplib
import unicodedata
import urllib.parse
import requests
from dotenv import load_dotenv

load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
MAPS_ACTOR = os.getenv("APIFY_MAPS_ACTOR", "compass/crawler-google-places").strip()
GOOGLE_ACTOR = os.getenv("APIFY_GOOGLE_ACTOR", "apify/google-search-scraper").strip()
# Renderande crawler (kör JS) — fallback för Wix/React-sajter där e-posten inte finns
# i rå HTML. Dyrare än plain scrape, så den körs BARA när vanlig skrapning gett noll.
RENDER_ACTOR = os.getenv("APIFY_RENDER_ACTOR", "apify/website-content-crawler").strip()

# run-sync väntar tills körningen är klar (max ~5 min). Bra för små scrapes.
RUN_TIMEOUT = 300

# Senaste Apify-felet i klartext (tomt = inget fel). Sätts av _run_actor så att
# UI:t kan säga t.ex. "krediterna slut" istället för att tyst hitta ingenting.
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


# ── Hemsidescrape (för personlig DM + people finder, gratis & säkert) ────────

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)

# Länkar som brukar leda till personer/roller på svenska SME-sajter.
_TEAM_HINTS = (
    "om-oss", "om_oss", "omoss", "kontakt", "contact", "medarbetare",
    "personal", "team", "ledning", "ledningsgrupp", "about", "people",
    "staff", "management", "organisation", "vart-team", "var-personal",
    # Extra svenska/engelska sidor som ofta listar namn + roller
    "styrelse", "vara-medarbetare", "vara-ledare", "om-foretaget",
    "kontaktpersoner", "kontaktperson", "vart-foretag", "foretaget",
    "who-we-are", "meet-the-team", "our-team", "our-people",
    "leadership", "executives", "board", "key-people", "directors",
    "ansvariga", "ansvarig", "chefer",
)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _get_html(url: str) -> str:
    """Hämta rå HTML (publik sida). Tom sträng vid fel."""
    url = _normalize_url(url)
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; LogisticsDoctorBot/1.0)"
        })
        if r.status_code != 200 or not r.text:
            return ""
        return r.text
    except Exception:
        return ""


def _strip_html(html: str, max_chars: int) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _HTML_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_chars]


def fetch_website_text(url: str, max_chars: int = 1500) -> str:
    """
    Hämta läsbar text från ett bolags startsida (publik, ingen LinkedIn).
    Används för personlig DM-kontext. Tom sträng vid fel.
    """
    return _strip_html(_get_html(url), max_chars)


def _team_page_urls(base_url: str, html: str, max_pages: int = 3) -> list[str]:
    """Plocka interna länkar som troligen leder till personer/kontakt/team."""
    base_url = _normalize_url(base_url)
    if not html:
        return []
    base = urllib.parse.urlparse(base_url)
    found: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        low = href.lower()
        if not any(h in low for h in _TEAM_HINTS):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        p = urllib.parse.urlparse(absolute)
        # Bara samma domän, hoppa mailto/tel/ankare
        if p.scheme not in ("http", "https") or p.netloc != base.netloc:
            continue
        clean = absolute.split("#")[0]
        if clean in seen:
            continue
        seen.add(clean)
        found.append(clean)
        if len(found) >= max_pages:
            break
    return found


def fetch_people_pages(url: str, max_pages: int = 6, max_chars: int = 4000) -> str:
    """
    Hämta text från bolagets person-/kontakt-/team-sidor (publikt, ingen LinkedIn).
    Startar på startsidan, följer interna 'om oss/kontakt/team'-länkar och slår ihop
    texten. Provar också vanliga kontaktsökvägar direkt (JS-menyer gömer ofta länkarna).
    """
    home = _get_html(url)
    if not home:
        return ""
    parts = [_strip_html(home, max_chars)]
    seen: set[str] = set()

    # Länkade team-/kontaktsidor
    for page in _team_page_urls(url, home, max_pages=max_pages):
        if page not in seen:
            seen.add(page)
            html = _get_html(page)
            if html:
                parts.append(f"\n[Sida: {page}]\n" + _strip_html(html, max_chars))

    # Prova vanliga sökvägar direkt (dolda bakom JS-meny)
    base = url.rstrip("/")
    for path in _COMMON_CONTACT_PATHS:
        cand = f"{base}/{path}"
        if cand not in seen:
            seen.add(cand)
            html = _get_html(cand)
            if html and len(html) > 500:   # ignorera 404-sidor som är nästan tomma
                parts.append(f"\n[Sida: {cand}]\n" + _strip_html(html, max_chars))
        if len(parts) > max_pages + 2:
            break

    return _WS_RE.sub(" ", " ".join(parts)).strip()[: max_chars * 3]


# ── E-post + hemsida (publik scraping, ingen LinkedIn) ──────────────────────

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Skräp att kasta: bibliotek/exempel/bildfiler/mallar, inte riktiga kontaktadresser.
_EMAIL_JUNK = (
    "example.com", "domain.com", "email.com", "yourdomain", "sentry", "wixpress",
    "wix.com", "godaddy", "schema.org", "@2x", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", "u003e", "u003c",
    # mall-/platshållaradresser
    "efternamn", "fornamn", "förnamn", "firstname", "lastname", "namn.namn",
)

# Lokaldel (före @) som signalerar vem mejlet går till — lägre index = högre prio.
_EMAIL_ROLE_PRIORITY = (
    "vd", "ceo", "ledning", "management", "direktor", "direktör", "chef", "owner",
    "sales", "forsaljning", "försäljning", "order", "info", "kontakt", "contact",
)

# Lågvärdiga adresser — behålls men trycks längst ner (sällan en väg in till ledning).
_EMAIL_LOWPRIO = (
    "noreply", "no-reply", "donotreply", "webmaster", "postmaster", "abuse",
    "gdpr", "dataskydd", "privacy", "whistleblower", "website", "newsletter",
    "press", "media", "jobb", "rekrytering", "career", "support", "faktura",
    "invoice", "billing",
)

_SOCIAL_DOMAINS = ("linkedin", "facebook", "instagram", "allabolag", "ratsit",
                   "eniro", "hitta.se", "youtube", "wikipedia", "blocket",
                   "twitter", "x.com", "google.", "merinfo")

# Vanliga kontaktsidor — provas direkt om de inte är länkade på startsidan (många
# sajter göms bakom JS-menyer, så länken syns inte i råa HTML:en).
_COMMON_CONTACT_PATHS = (
    "kontakt", "kontakta-oss", "om-oss", "om", "contact",
    "about", "kontakt-oss", "foretaget", "medarbetare",
    # Extra sökvägar för att hitta namn + roller
    "ledning", "personal", "styrelse", "team", "management",
    "om-foretaget", "vart-team", "about-us", "leadership",
    "kontaktpersoner", "ansvariga", "vara-medarbetare",
)

# Cloudflare-mejlskydd: adressen ligger hex-kodad i data-cfemail / #-länk.
_CFEMAIL_RE = re.compile(r'data-cfemail=["\']([0-9a-fA-F]+)["\']')
_CFEMAIL_LINK_RE = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')

# Text-maskerade adresser: "info [at] foretag punkt se", "kontakt (snabel-a) ..."
_AT_OBFUSC = r'\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+|\[snabel-a\]|\(snabel-a\)|\s+snabel-?a\s+)\s*'
_DOT_OBFUSC = r'\s*(?:\[dot\]|\(dot\)|\[punkt\]|\(punkt\)|\s+dot\s+|\s+punkt\s+)\s*'
_OBFUSC_EMAIL_RE = re.compile(
    r'([A-Za-z0-9._%+-]+)' + _AT_OBFUSC + r'([A-Za-z0-9.-]+)' + _DOT_OBFUSC + r'([A-Za-z]{2,})',
    re.IGNORECASE,
)


def _decode_cfemail(hex_str: str) -> str:
    """Avkoda en Cloudflare-skyddad mejladress (data-cfemail). '' vid fel."""
    try:
        data = bytes.fromhex(hex_str)
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except Exception:
        return ""


def _crawl_rendered(urls: list[str], max_pages: int = 6) -> list[str]:
    """
    Rendera JS-tunga sidor via Apify (website-content-crawler) och returnera den
    färdig-renderade HTML:en + texten per sida. När JS körts har Cloudflare-skyddet
    redan ersatt '[email protected]' med riktig adress i DOM:en, och Wix/React-innehåll
    finns på plats. Tom lista vid fel/ej konfigurerat — anroparen faller då tillbaka.

    maxCrawlDepth=0 → bara de URL:er vi skickar in (inga länkar följs), så kostnaden
    är förutsägbar: max len(urls) sidor.
    """
    urls = [u for u in urls if u]
    if not urls or not is_configured():
        return []
    run_input = {
        "startUrls": [{"url": u} for u in urls[:max_pages]],
        "maxCrawlPages": int(max_pages),
        "maxCrawlDepth": 0,
        "crawlerType": "playwright:firefox",
        "saveHtml": True,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    items = _run_actor(RENDER_ACTOR, run_input)
    blocks: list[str] = []
    for it in items:
        if it.get("html"):
            blocks.append(str(it["html"]))
        if it.get("text"):
            blocks.append(str(it["text"]))
    return blocks


def _extract_emails_from_html(html: str) -> list[str]:
    """Plocka alla mejladresser ur HTML: rena, Cloudflare-kodade och text-maskerade."""
    out: list[str] = []
    if not html:
        return out
    # 1. Rena adresser (även i mailto:)
    out.extend(_EMAIL_RE.findall(html))
    # 2. Cloudflare-skyddade
    for hexcode in _CFEMAIL_RE.findall(html) + _CFEMAIL_LINK_RE.findall(html):
        dec = _decode_cfemail(hexcode)
        if dec and "@" in dec:
            out.append(dec)
    # 3. Text-maskerade ("info [at] foretag punkt se")
    for local, dom, tld in _OBFUSC_EMAIL_RE.findall(html):
        out.append(f"{local}@{dom}.{tld}")
    return out


# ── Telefonuttag (gratis, ur samma HTML som mejlen) ─────────────────────────────

_TEL_HREF_RE = re.compile(r'href=["\']tel:([+0-9()\s\-.]{6,})["\']', re.IGNORECASE)
# Svenska nummer i text: +46 eller 0, sedan 7–9 siffror med vanliga avskiljare.
_PHONE_TEXT_RE = re.compile(
    r'(?<![\w./-])(?:\+46[\s\-]?|0)(?:\d[\s\-.]?){6,9}\d(?![\w/])')


def _clean_phone(raw: str) -> str:
    """Normalisera ett rånummer. '' om det inte ser ut som ett riktigt telefonnr."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("0046"):
        s = "+46" + s[4:]
    if s.startswith("+460"):            # redundant riktnolla efter landskod
        s = "+46" + s[4:]
    if not (s.startswith("+") or s.startswith("0")):   # dialbart nr, inte t.ex. orgnr
        return ""
    digits = re.sub(r"\D", "", s)
    if not (8 <= len(digits) <= 12):
        return ""
    return s


def _extract_phones(html: str) -> list[str]:
    """Plocka telefonnummer ur HTML: tel:-länkar först, annars svenska textnummer."""
    if not html:
        return []
    found = [_clean_phone(m) for m in _TEL_HREF_RE.findall(html)]
    found = [f for f in found if f]
    if not found:
        found = [c for m in _PHONE_TEXT_RE.findall(html) if (c := _clean_phone(m))]
    seen, out = set(), []
    for p in found:
        key = re.sub(r"\D", "", p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    def _mobile_first(p):  # mobil (07 / +46 7x) före fast telefon
        d = re.sub(r"\D", "", p)
        return 0 if (d.startswith("467") or d.startswith("07")) else 1
    out.sort(key=_mobile_first)
    return out[:5]


def find_company_website(bolag: str) -> str:
    """Hitta ett bolags hemsida via Google (hoppar sociala medier/register). '' om inget."""
    bolag = (bolag or "").strip()
    if not bolag or not is_configured():
        return ""
    for hit in google_search(f"{bolag} kontakt", max_results=6):
        netloc = urllib.parse.urlparse(_normalize_url(hit.get("url", ""))).netloc.lower()
        if not netloc or any(s in netloc for s in _SOCIAL_DOMAINS):
            continue
        return f"https://{netloc}"
    return ""


# ── Gratis hemsidegissning (ingen Apify) ────────────────────────────────────────

# Rena bolagsformer som aldrig är del av en domän — tas bort ur namnet.
_LEGAL_TOKENS = frozenset({
    "ab", "aktiebolag", "hb", "handelsbolag", "kb", "kommanditbolag",
    "ekonomisk", "forening", "ideell", "asa", "oy", "as", "gmbh", "ltd",
    "inc", "plc", "bv", "publ",
})
# Sidor som visar att domänen är parkerad/till salu → ingen riktig hemsida.
_PARKED_HINTS = (
    "this domain is for sale", "köp denna domän", "domänen är till salu",
    "parkerad", "domain parking", "buy this domain", "sedoparking",
    "domännamnet är ledigt",
)
# För generiska/geografiska ord ensamma → domänen blir nästan alltid fel bolag
# (t.ex. "Swedish Microwave" → swedish.com). Används aldrig som ensam stam.
_GENERIC_WORDS = frozenset({
    "swedish", "sweden", "nordic", "nordics", "scandinavia", "scandinavian",
    "european", "europe", "euro", "scan", "global", "international", "svenska",
})


def _company_domain_stems(bolag: str) -> list[str]:
    """Troliga domän-stammar från ett bolagsnamn, mest sannolik först."""
    words = [w for w in re.split(r"[^a-z0-9]+", _ascii_name(bolag)) if w]
    words = [w for w in words if w not in _LEGAL_TOKENS] or words
    stems: list[str] = []
    if words:
        if words[0] not in _GENERIC_WORDS and len(words[0]) >= 3:
            stems.append(words[0])             # 'rottne'  (vanligast för SME)
        if len(words) >= 2:
            stems.append(words[0] + words[1])  # 'rottneindustri'
            stems.append("".join(words[:-1]))  # allt utom sista beskrivande ordet
        stems.append("".join(words))           # alla ord hopslagna
        stems.append("-".join(words))          # bindestreck
    # Rensa: minst 3 tecken och aldrig ett ensamt generiskt ord (t.ex. 'swedish').
    return list(dict.fromkeys(
        s for s in stems if len(s) >= 3 and s not in _GENERIC_WORDS))


def _probe(url: str, timeout: int = 5) -> str:
    """Snabb HTTP-hämtning (kort timeout) för domängissning. Tom sträng vid fel."""
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; LogisticsDoctorBot/1.0)"})
        return r.text if (r.status_code == 200 and r.text) else ""
    except Exception:
        return ""


def _page_matches_company(html: str, bolag: str) -> bool:
    """True om sidan rimligt hör till bolaget (och inte är en parkerad domän)."""
    low = html.lower()
    if any(bad in low for bad in _PARKED_HINTS):
        return False
    tokens = [w for w in re.split(r"[^a-z0-9]+", _ascii_name(bolag))
              if len(w) >= 4 and w not in _LEGAL_TOKENS]
    return any(t in low for t in tokens[:2]) if tokens else True


def guess_company_website(bolag: str, max_probes: int = 12) -> str:
    """
    Gissa och VERIFIERA ett bolags hemsida GRATIS (ingen Apify): prova troliga
    domäner ur namnet och läs dem med vanlig HTTP. Returnerar en URL bara om
    sidan svarar OCH rimligt matchar bolaget, annars "". Perfekt för svenska SME
    vars domän matchar bolagsnamnet.
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return ""
    probes = 0
    for stem in _company_domain_stems(bolag):
        for tld in (".se", ".com", ".nu"):
            # Prova både www och naken domän (vissa sajter serveras bara på apex).
            for url in (f"https://www.{stem}{tld}", f"https://{stem}{tld}"):
                if probes >= max_probes:
                    return ""
                probes += 1
                html = _probe(url)
                if html and _page_matches_company(html, bolag):
                    return url
    return ""


def _rank_emails(emails: list[str], website: str) -> list[str]:
    domain = urllib.parse.urlparse(_normalize_url(website)).netloc.lower().replace("www.", "")

    def key(e: str):
        local, _, dom = e.partition("@")
        score = 0
        if domain and domain in dom:        # adresser på bolagets egen domän först
            score -= 100
        if any(kw in local for kw in _EMAIL_LOWPRIO):
            score += 200                     # noreply/whistleblower/support etc. längst ner
        for i, kw in enumerate(_EMAIL_ROLE_PRIORITY):
            if kw in local:
                score -= (40 - i)            # tidig roll i listan = högre prio
                break
        return score
    return sorted(emails, key=key)


def find_emails(website: str = "", bolag: str = "", render: bool = False) -> dict:
    """
    Leta publika e-postadresser på ett bolags hemsida (startsida + kontakt/om-oss/
    ledningssidor). Backup-väg in om LinkedIn inte funkar. Rör aldrig LinkedIn.

    Hanterar även Cloudflare-skyddade och text-maskerade adresser ("info [at] ...").
    Provar dessutom vanliga kontaktsidor direkt (/kontakt, /om-oss) ifall länken är
    gömd bakom en JS-meny. Hittas ingen riktig adress gissas info@domän (markeras).

    render=True: om vanlig (gratis) skrapning ger NOLL adresser körs en renderande
    Apify-crawler som kör JS — fångar Wix/React-sajter och Cloudflare-skydd som plain
    HTTP missar. Körs bara som sista utväg, så krediter dras bara på de svåra sajterna.

    Ange website (snabbast) eller bolag (slås upp via Google). Returnerar:
      {"website": url, "emails": [...], "best": str, "guessed": str, "rendered": bool}
    'guessed' är en kvalificerad gissning (info@domän) — bara satt när inget hittades.
    """
    website = _normalize_url(website)
    if not website and bolag:
        # Gratis gissning först (ingen Apify), betald Google-sökning bara som fallback.
        website = guess_company_website(bolag) or find_company_website(bolag)
    if not website:
        return {"website": "", "emails": [], "best": "", "guessed": "", "rendered": False}

    home = _get_html(website)

    # Samla kandidatsidor: startsida + länkade team-/kontaktsidor + vanliga sökvägar.
    pages: list[str] = []
    seen_urls: set[str] = set()
    for page in _team_page_urls(website, home, max_pages=4):
        if page not in seen_urls:
            seen_urls.add(page)
            pages.append(page)
    base = website.rstrip("/")
    for path in _COMMON_CONTACT_PATHS:
        cand = f"{base}/{path}"
        if cand not in seen_urls:
            seen_urls.add(cand)
            pages.append(cand)

    seen: set[str] = set()
    emails: list[str] = []

    def _harvest(html_blocks: list[str]) -> None:
        for h in html_blocks:
            for raw in _extract_emails_from_html(h):
                e = raw.strip().strip(".").lower()
                if not e or e in seen or any(j in e for j in _EMAIL_JUNK):
                    continue
                seen.add(e)
                emails.append(e)

    # Steg 1 — gratis plain-HTTP-skrapning.
    plain = [home] if home else []
    for page in pages[:10]:
        h = _get_html(page)
        if h:
            plain.append(h)
    _harvest(plain)
    all_html = list(plain)

    # Steg 2 — renderande fallback (kör JS) bara om plain gav noll OCH render begärts.
    rendered = False
    if not emails and render and is_configured():
        render_urls = [website] + [u for u in pages if u.startswith(base)][:5]
        blocks = _crawl_rendered(render_urls, max_pages=6)
        if blocks:
            rendered = True
            _harvest(blocks)
            all_html.extend(blocks)

    emails = _rank_emails(emails, website)
    # Telefon plockas gratis ur samma HTML (tel:-länkar prioriteras).
    telefoner = _extract_phones("\n".join(all_html))

    # Fallback: ingen publik adress hittad → gissa info@bolagets-domän (vanligast i SME).
    guessed = ""
    if not emails:
        domain = urllib.parse.urlparse(website).netloc.lower().replace("www.", "")
        if domain and "." in domain:
            guessed = f"info@{domain}"

    return {"website": website, "emails": emails[:10],
            "best": emails[0] if emails else "", "guessed": guessed,
            "telefon": telefoner[0] if telefoner else "", "telefoner": telefoner,
            "rendered": rendered}


# ── E-postmönster-konstruktion (för namngiven person) ───────────────────────────

# Konverterar svenska tecken till ASCII för e-postgenerering.
_SWE_MAP = str.maketrans("åäöüéèêàÅÄÖÜÉÈÊÀ", "aaoueeeaAAOUEEEA")


def _ascii_name(s: str) -> str:
    """'Karin Söderqvist' → 'karin soderqvist' (ASCII, lowercase)."""
    s = s.lower().translate(_SWE_MAP)
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


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


# ── Google-sökning (för publika LinkedIn-profiler, via Apify — ej ditt konto) ─

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
