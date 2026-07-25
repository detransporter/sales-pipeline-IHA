"""
Hemsidescrape (gratis, publikt, ingen LinkedIn) — hämtar och läser text från
ett bolags hemsida: startsidan, team-/kontaktsidor hittade via länkar eller
sitemap.xml, och strukturdata (schema.org). Används för personlig DM-kontext
(fetch_website_text) och som förarbete åt people_finder/e-postsök
(fetch_people_pages). Ingen Apify här — bara vanlig HTTP.

Del av `integrations/apify_research` — se paketets `__init__.py`.
"""

import json
import re
import urllib.parse

import requests

# BeautifulSoup ger strukturbevarande parsning (namn/titel/mejl hålls ihop rad
# för rad, bild-alt-texter följer med). Tålig import — utan bs4 körs regex-strip.
try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None


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

# Vanliga kontaktsidor — provas direkt om de inte är länkade på startsidan (många
# sajter göms bakom JS-menyer, så länken syns inte i råa HTML:en). Delad mellan
# fetch_people_pages (den här filen) och ._contact.find_emails — samma sökvägar
# är relevanta för båda.
_COMMON_CONTACT_PATHS = (
    "kontakt", "kontakta-oss", "om-oss", "om", "contact",
    "about", "kontakt-oss", "foretaget", "medarbetare",
    # Extra sökvägar för att hitta namn + roller
    "ledning", "personal", "styrelse", "team", "management",
    "om-foretaget", "vart-team", "about-us", "leadership",
    "kontaktpersoner", "ansvariga", "vara-medarbetare",
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
        # Webbläsarlika headers — botskydd blockerar ofta okända robotar men
        # släpper igenom vanliga webbläsare (viktigt från Streamlit Cloud).
        r = requests.get(url, timeout=15, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
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


def _soup_text(html: str, max_chars: int) -> str:
    """
    Läsbar text ur HTML med bibehållen struktur: radbrytningar mellan block
    (så "Anna Andersson" och "Inköpschef" hålls ihop som grannrader i stället
    för att drunkna i en ordsoppa), bild-alt-texter (teamfoton bär ofta namnet
    där) och mailto-adresser (står ofta bara i href, inte i länktexten).
    Fallback till regex-strip om bs4 saknas eller HTML:en är trasig.
    """
    if not BeautifulSoup:
        return _strip_html(html, max_chars)
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").strip()
            if 3 < len(alt) < 120:
                img.replace_with(f" [bild: {alt}] ")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                if addr and addr not in a.get_text():
                    a.append(f" <{addr}>")
        lines = [ln.strip() for ln in soup.get_text(separator="\n").splitlines()]
        return "\n".join(ln for ln in lines if ln)[:max_chars]
    except Exception:
        return _strip_html(html, max_chars)


def _jsonld_people(html: str) -> list[dict]:
    """
    Läs schema.org-strukturdata (JSON-LD) ur sidan — där ligger ibland namn +
    roll färdigt maskinläsbart (@type Person, ofta under employee/founder).
    Returnerar [{namn, titel, email}], tom lista om inget finns.
    """
    if not BeautifulSoup or not html:
        return []
    people: list[dict] = []

    def _walk(node):
        if isinstance(node, dict):
            if "person" in str(node.get("@type", "")).lower():
                namn = str(node.get("name", "")).strip()
                if namn:
                    people.append({
                        "namn": namn,
                        "titel": str(node.get("jobTitle", "")).strip(),
                        "email": str(node.get("email", "")).replace("mailto:", "").strip(),
                    })
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                _walk(json.loads(tag.string or ""))
            except Exception:
                continue
    except Exception:
        return []
    seen: set[str] = set()
    out = []
    for p in people:
        k = p["namn"].lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out[:15]


def _sitemap_team_urls(base_url: str, max_urls: int = 5) -> list[str]:
    """
    Hitta team-/kontaktsidor via sitemap.xml — fångar sidor som göms bakom
    JS-menyer där länkjakten i HTML aldrig ser dem.
    """
    base = _normalize_url(base_url).rstrip("/")
    xml = _get_html(f"{base}/sitemap.xml")
    if not xml:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    # Sitemap-index pekar på under-sitemaps — följ några av dem.
    for sub in [u for u in locs if u.lower().endswith(".xml")][:3]:
        sub_xml = _get_html(sub)
        if sub_xml:
            locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub_xml)
    # Starka ledtrådar (pekar nästan alltid på personer) prioriteras före svaga
    # som "about"/"om" (matchar även nyhetssidor under /about-.../news/).
    strong = ("kontakt", "contact", "team", "ledning", "medarbetare", "personal",
              "management", "styrelse", "people", "staff", "leadership")
    noise = ("/news", "/nyheter", "/blog", "/blogg", "/press", "/event", "/karriar", "/career")
    strong_hits: list[str] = []
    weak_hits: list[str] = []
    for u in locs:
        low = u.lower()
        # .xml är under-sitemaps, inte sidor — de har redan följts ovan.
        if low.endswith(".xml") or any(n in low for n in noise):
            continue
        if any(h in low for h in strong):
            strong_hits.append(u)
        elif any(h in low for h in _TEAM_HINTS):
            weak_hits.append(u)
    out: list[str] = []
    for u in strong_hits + weak_hits:
        if u not in out:
            out.append(u)
        if len(out) >= max_urls:
            break
    return out


def fetch_website_text(url: str, max_chars: int = 1500) -> str:
    """
    Hämta läsbar text från ett bolags startsida (publik, ingen LinkedIn).
    Används för personlig DM-kontext. Tom sträng vid fel.
    """
    return _soup_text(_get_html(url), max_chars)


def _team_page_urls(base_url: str, html: str, max_pages: int = 3) -> list[str]:
    """Plocka interna länkar som troligen leder till personer/kontakt/team."""
    base_url = _normalize_url(base_url)
    if not html:
        return []
    base = urllib.parse.urlparse(base_url)
    # www. avskalat innan jämförelse — sajter som omdirigerar apex→www (t.ex.
    # vimek.com → www.vimek.com) har alla interna länkar i www.-form, vilket en
    # exakt strängjämförelse annars kastar bort som "fel domän" (Vimek AB-fallet,
    # samma bugg som i agents/people_finder.py:_discover_team_links).
    base_host = base.netloc.lower().removeprefix("www.")
    found: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        low = href.lower()
        if not any(h in low for h in _TEAM_HINTS):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        p = urllib.parse.urlparse(absolute)
        # Bara samma domän, hoppa mailto/tel/ankare
        if (p.scheme not in ("http", "https")
                or p.netloc.lower().removeprefix("www.") != base_host):
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
        print(f"[scrape] {url}: startsidan gick inte att hämta")
        return ""
    parts = [_soup_text(home, max_chars)]
    seen: set[str] = set()

    # Strukturdata (schema.org) — namn + roll färdigt maskinläsbart när det finns.
    ld_people = _jsonld_people(home)

    # Länkade team-/kontaktsidor + sidor ur sitemap.xml (fångar JS-gömda menyer)
    pages = _team_page_urls(url, home, max_pages=max_pages)
    for extra in _sitemap_team_urls(url):
        if extra not in pages:
            pages.append(extra)
    for page in pages[:max_pages]:
        if page not in seen:
            seen.add(page)
            html = _get_html(page)
            if html:
                ld_people += [p for p in _jsonld_people(html) if p not in ld_people]
                parts.append(f"\n[Sida: {page}]\n" + _soup_text(html, max_chars))

    # Prova vanliga sökvägar direkt (dolda bakom JS-meny)
    base = url.rstrip("/")
    for path in _COMMON_CONTACT_PATHS:
        cand = f"{base}/{path}"
        if cand not in seen:
            seen.add(cand)
            html = _get_html(cand)
            if html and len(html) > 500:   # ignorera 404-sidor som är nästan tomma
                ld_people += [p for p in _jsonld_people(html) if p not in ld_people]
                parts.append(f"\n[Sida: {cand}]\n" + _soup_text(html, max_chars))
        if len(parts) > max_pages + 2:
            break

    # Lägg strukturdata-personerna FÖRST — säkraste källan, Claude ska se den direkt.
    if ld_people:
        parts.insert(0, "PERSONER FRÅN STRUKTURDATA (schema.org — pålitlig källa):\n" + "\n".join(
            f"- {p['namn']}"
            + (f" — {p['titel']}" if p["titel"] else "")
            + (f" <{p['email']}>" if p["email"] else "")
            for p in ld_people
        ))

    text = "\n".join(parts).strip()[: max_chars * 3]
    # Mätlogg: tom/kort text = troligen JS-renderad sajt (syns i konsolen).
    if len(text) < 300:
        print(f"[scrape] {url}: bara {len(text)} tecken text — troligen JS-renderad sajt")
    return text
