"""
People-finder — hittar rätt PERSON att kontakta på ett bolag genom att läsa
bolagets egen hemsida, precis som David gör manuellt.

Metod (Claude-läsning som förstahandsval — ingen Apify, ingen LinkedIn):
  1. Hämta text från de mest sannolika undersidorna (/kontakt, /kontakta-oss,
     /om-oss, /ledning, ...) — provar i tur och ordning och samlar upp till
     tre sidor med substantiellt innehåll.
  2. Ger ingen undersida träff: en enkel Claude web search får peka ut rätt
     sida ("[bolag]" kontakt VD OR inköpschef OR CFO), som sedan hämtas.
  3. Claude LÄSER texten och identifierar namn kopplat till titel — det är
     läsförståelse, inte skrapning. Prioritet: VD/CEO > CFO/Ekonomichef >
     Inköpschef/Supply Chain > annan ledningsperson. Gissar aldrig — finns
     inget namn i texten returneras tomt.

Varför inte Apify/LinkedIn längre: Apify-actorer är byggda för kontaktformulär
och info@-adresser, inte för att koppla "Anna Svensson, Inköpschef" i löptext
till en roll. LinkedIn gav sällan träffar för svenska SME i målgruppen.

Open Brain-inlärning (tåligt — fel loggar aldrig sönder sökningen):
  - Före sökning: hämtar tidigare lärdomar för branschen; nämner en lärdom en
    undersida (t.ex. /ledning) provas den först.
  - Efter sökning: sparar kort notering (bolag, vilken sida som gav träff,
    om person hittades).
  - Rättningar fångas i views/leads.py (när David skriver över en felgissning).
Open Brain nås via brain/open_brain.py (JSON-RPC över HTTP) — INTE via MCP.
"""

import os
import re
import json
import urllib.parse
import requests
import anthropic
from dotenv import load_dotenv

# BeautifulSoup ger strukturbevarande text (namn/titel/mejl hålls ihop rad för
# rad, bild-alt-texter följer med). Tålig import — utan bs4 körs regex-strip.
try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

# Open Brain-klient (tålig import — appen funkar även utan minnet).
try:
    from brain import open_brain as brain
except Exception:  # pragma: no cover
    brain = None

load_dotenv()

MODEL = "claude-sonnet-4-6"          # webbsök (web_search-verktyget kräver Sonnet)
# Själva läsningen är enkel texttolkning (namn+titel ur sidtext) — Haiku klarar
# den lika bra till en tredjedel av kostnaden. Volymdrivaren i bulk-körningar.
READ_MODEL = "claude-haiku-4-5"

# Undersidor i sannolikhetsordning — samma sidor David själv kollar manuellt.
# Tom sträng = startsidan, som sista utväg.
CANDIDATE_PATHS = (
    "kontakt", "kontakta-oss", "om-oss", "om-foretaget", "ledning",
    "organisation", "medarbetare", "team", "contact", "about", "",
)

# Tak per bolag så en bulk-körning på 20+ leads inte springer iväg:
MAX_FETCHES = 8      # max antal HTTP-hämtningar
MAX_PAGES_TO_READ = 3  # max antal sidor som skickas till Claude (en läsning totalt)
MIN_PAGE_CHARS = 400   # under detta räknas sidan som tom (JS-skal, 404-sida)


READ_SYSTEM = """Du är en research-assistent för Baris AB / Logistics Doctor (David Leifsson).
David säljer IHA (lager-/kapitalbindningsanalys) till svenska SME-bolag och behöver veta
VEM på bolaget han ska kontakta. Du får text från bolagets egen hemsida (Kontakt/Om oss/
Ledning-sidor). Din uppgift är RENODLAD LÄSFÖRSTÅELSE: hitta personer där ett namn står
kopplat till en titel/roll i texten.

PRIORITERA i denna ordning:
1. CFO/Ekonomichef/Ekonomiansvarig (även bara "Ekonomi" som avdelningsetikett)
2. Inköpschef/Inköpsansvarig/Supply Chain Manager (även bara "Inköp")
3. Logistikchef/Lagerchef/Operations Manager/COO
4. VD/CEO (först när ingen ovan finns — i småbolag äger VD ofta ekonomin/inköpet)
Undvik HR, marknad, sälj, IT, kundtjänst, produktion.

OBS: På kontaktsidor står rollen ofta som kort avdelningsetikett OVANFÖR eller
BREDVID namnet ("EKONOMI", "INKÖP", "VD") — koppla etiketten till namnet under/intill.

HÅRDA REGLER:
- Hitta ALDRIG på en person. Använd bara namn som faktiskt står i texten.
- Namnet måste stå kopplat till en titel/roll i texten — ett namn utan roll är
  bara värt att returnera om ingen person med roll finns (sätt då sakerhet "låg").
- Står e-post eller telefon i DIREKT anslutning till personen: ta med dem.
  Ta INTE generiska adresser (info@, order@, växelnummer) som personens.
- Hittar du ingen lämplig person: returnera "namn": "".
- I "kalla_url": ange URL:en till den sida (av de märkta [Sida: ...]) där du
  faktiskt hittade personen.

Returnera ENDAST JSON:
{
  "namn": "För- och efternamn (bästa valet), eller tom sträng",
  "titel": "personens roll som den står i texten",
  "email": "personens egen e-post om den står vid namnet, annars tom",
  "telefon": "personens eget nummer om det står vid namnet, annars tom",
  "kalla_url": "URL till sidan där personen hittades",
  "sakerhet": "hög | medel | låg",
  "motivering": "kort: varför denna person",
  "kandidater": [
    {"namn": "...", "titel": "...", "email": "...", "telefon": "..."}
  ]
}
"kandidater" = ALLA personer med namn + roll du hittade i texten (max 8), i
prioritetsordning med bästa valet först — även roller utanför prioriteringen
(VD, platschef, sälj) så David själv kan välja. Samma regler: bara namn som
står i texten, mejl/telefon bara om de står vid personens namn.
Ingen text utanför JSON."""


# ── Småhjälpare ────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        # Sista utväg: plocka ut första {...}-blocket ur ev. omgivande text.
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                pass
        return {}


def _log(msg: str) -> None:
    """Konsollogg så David ser vilken sida som lästes (appen har ingen logging-setup)."""
    print(msg)


def _normalize_url(url: str) -> str:
    """
    Normalisera + uppgradera till https. Sparade leads har ibland http:// från
    äldre gissningar — många moderna sajter (Cloudflare m.fl.) svarar inte alls
    på ren http, vilket gav 8 anslutningsfel i rad innan sökningen gav upp
    (Olle Svensson-fallet: 82 sek åtgången till stor del på just detta).
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("http"):
        url = "https://" + url
    return url.rstrip("/")


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)


# Webbläsarlika headers — sajter (och deras botskydd) blockerar ofta okända
# robotar men släpper igenom vanliga webbläsare. Detta gjorde att sökningen
# funkade lokalt men inte från Streamlit Clouds servrar (Olle Svensson-fallet).
_FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}


def _fetch_html(url: str) -> str:
    """Hämta rå HTML. Tom sträng vid fel (statusen loggas för felsökning)."""
    try:
        # 8 sek (inte 15) — sidor som svarar inom rimlig tid gör det inom
        # ett par sekunder; en trög/blockerad sajt ska inte hänga sökningen.
        r = requests.get(url, timeout=8, headers=_FETCH_HEADERS)
        if r.status_code != 200 or not r.text:
            _log(f"[people_finder] {url}: HTTP {r.status_code}, "
                 f"{len(r.text or '')} tecken")
            return ""
        return r.text
    except Exception as e:
        _log(f"[people_finder] {url}: hämtning misslyckades ({type(e).__name__})")
        return ""


# Länkord som pekar mot personer — starka (nästan alltid rätt) före svaga.
_LINK_STRONG = ("ledning", "ledningsgrupp", "styrelse", "medarbetare", "personal",
                "team", "management", "leadership", "kontakt", "contact")
_LINK_WEAK = ("om-oss", "om_oss", "omoss", "about", "organisation", "om-foretaget",
              "foretaget", "people", "staff")


def _discover_team_links(base: str, html: str, max_links: int = 5) -> list[str]:
    """
    Följ menylänkarna på startsidan till Om oss/Ledning/Kontakt — samma sak som
    David gör manuellt. Löser sajter med språkprefix (/se/om-oss/...) där de
    hårdkodade sökvägarna bommar.
    """
    if not html:
        return []
    import urllib.parse
    base_host = urllib.parse.urlparse(base).netloc
    strong: list[str] = []
    weak: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        absolute = urllib.parse.urljoin(base + "/", href).split("#")[0].rstrip("/")
        p = urllib.parse.urlparse(absolute)
        if p.scheme not in ("http", "https") or p.netloc != base_host:
            continue
        if absolute in seen or absolute == base:
            continue
        seen.add(absolute)
        low = absolute.lower()
        if any(w in low for w in _LINK_STRONG):
            strong.append(absolute)
        elif any(w in low for w in _LINK_WEAK):
            weak.append(absolute)
    return (strong + weak)[:max_links]


def _fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Hämta en sida och ge ren, strukturbevarande text. Tom sträng vid fel."""
    html = _fetch_html(url)
    if not html:
        return ""
    if not BeautifulSoup:
        text = _HTML_RE.sub(" ", _TAG_RE.sub(" ", html))
        return _WS_RE.sub(" ", text).strip()[:max_chars]
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        # Meny/navigering bort — på stora sajter äter megamenyn hela teckenbudgeten
        # och trycker ut personnamnen. Huvudinnehållet ligger i <main> när det finns.
        for tag in soup(["nav", "header", "aside"]):
            tag.decompose()
        main = soup.find("main")
        if main and len(main.get_text(strip=True)) > 200:
            soup = main
        # Teamfoton bär ofta namnet i alt-texten — gör det synligt för Claude.
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").strip()
            if 3 < len(alt) < 120:
                img.replace_with(f" [bild: {alt}] ")
        # Mejladresser ligger ofta bara i mailto-länken, inte i länktexten.
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                if addr and addr not in a.get_text():
                    a.append(f" <{addr}>")
        lines = [ln.strip() for ln in soup.get_text(separator="\n").splitlines()]
        return "\n".join(ln for ln in lines if ln)[:max_chars]
    except Exception:
        text = _HTML_RE.sub(" ", _TAG_RE.sub(" ", html))
        return _WS_RE.sub(" ", text).strip()[:max_chars]


# ── Open Brain-inlärning (tålig — fel får aldrig stoppa sökningen) ──────────────

def _recall_strategy(bransch: str, bolag: str) -> str:
    """Hämta tidigare lärdomar om personsökning för liknande bolag. '' om inget/fel."""
    if not brain or not brain.is_configured():
        return ""
    try:
        query = (f"people_finder strategi kontaktperson {bransch} SME "
                 f"var ledning/inköp finns").strip()
        res = brain.search_thoughts(query)
        return (res or "").strip()[:800]
    except Exception:
        return ""


def _order_paths(strategy: str) -> list[str]:
    """Nämner en lärdom en specifik undersida (t.ex. /ledning) — prova den först."""
    paths = list(CANDIDATE_PATHS)
    if not strategy:
        return paths
    low = strategy.lower()
    hinted = [p for p in paths if p and p in low]
    return hinted + [p for p in paths if p not in hinted]


def _remember_outcome(bolag: str, bransch: str, hit_url: str, found_namn: str,
                      titel: str = "") -> None:
    """Spara en kort notering om utfallet (för framtida sökningar). Tyst vid fel."""
    if not brain or not brain.is_configured():
        return
    try:
        var = f" på {hit_url}" if hit_url else ""
        if found_namn:
            body = (f"[people_finder] {bolag} ({bransch or 'okänd bransch'}): "
                    f"hittade {found_namn}"
                    + (f", {titel}" if titel else "") + f"{var} genom att läsa hemsidan.")
        else:
            body = (f"[people_finder] {bolag} ({bransch or 'okänd bransch'}): "
                    f"ingen person med roll i hemsidetexten{var}. "
                    f"Prova annan undersida nästa gång.")
        brain.capture_thought(body[:400])
    except Exception:
        pass


# ── Web search-reserv: hitta rätt SIDA när URL-mönstren inte ger något ──────────

def _search_contact_page(bolag: str) -> str:
    """
    Låt Claude web search peka ut bolagets kontakt-/ledningssida.
    Returnerar en URL eller tom sträng. Själva läsningen sker separat.
    """
    user = (
        f'Hitta det svenska bolaget "{bolag}"s EGEN webbplats och där URL:en till '
        f"deras kontakt-, ledning- eller om oss-sida.\n"
        f"VIKTIGT: varumärket skiljer sig ofta från det juridiska namnet — "
        f'"Meson AB" kan heta mesongroup.com, "Svensson Verktyg AB" kan heta '
        f"svenssons.se. Sök först på bara bolagsnamnet om en snävare sökning inte "
        f"ger något. Ta INTE allabolag/hitta.se/ratsit/LinkedIn/sociala medier — "
        f"bara bolagets egen domän.\n"
        f'Returnera ENDAST JSON: {{"url": "https://... eller tom sträng"}}'
    )
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
        messages = [{"role": "user", "content": user}]
        resp = client.messages.create(model=MODEL, max_tokens=500,
                                      tools=tools, messages=messages)
        # server-side tool-loop kan pausa (pause_turn) — återuppta max 2 gånger
        conts = 0
        while getattr(resp, "stop_reason", None) == "pause_turn" and conts < 2:
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": resp.content}]
            resp = client.messages.create(model=MODEL, max_tokens=500,
                                          tools=tools, messages=messages)
            conts += 1
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        url = str(_parse_json(text).get("url", "")).strip()
        return url if url.startswith("http") else ""
    except Exception as e:
        _log(f"[people_finder] web search efter kontaktsida misslyckades för {bolag}: {e}")
        return ""


# ── Kärnan: Claude läser hemsidetexten ──────────────────────────────────────────

def _read_pages(bolag: str, bransch: str, target_role: str,
                pages: list[tuple[str, str]], strategy: str) -> dict:
    """Skicka de hämtade sidorna till Claude för läsning. pages = [(url, text)]."""
    pages_block = "\n\n".join(f"[Sida: {u}]\n{t}" for u, t in pages)
    strat_block = (f"\n\nMINNE — tidigare lärdomar (använd bara om relevant):\n{strategy}"
                   if strategy else "")
    user_message = (
        f"Bolag: {bolag}\n"
        f"Bransch: {bransch or 'okänd'}\n"
        f"Roll vi helst vill nå: {target_role or '(ospecificerad — följ prioritetsordningen)'}"
        f"{strat_block}\n\n"
        f"TEXT FRÅN BOLAGETS HEMSIDA:\n{pages_block}\n\n"
        f"Identifiera bästa person. Returnera JSON."
    )
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=READ_MODEL,
            max_tokens=600,
            system=READ_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        return _parse_json(text)
    except Exception as e:
        _log(f"[people_finder] Claude-läsning misslyckades för {bolag}: {e}")
        return {}


# ── Publikt gränssnitt ─────────────────────────────────────────────────────────

def find_person(bolag: str, website: str = "", target_role: str = "",
                bransch: str = "") -> dict:
    """
    Hitta bästa person att kontakta genom att LÄSA bolagets hemsida (Claude).
    Returnerar dict — namn tomt om inget hittas. Nycklar som anropare läser
    (namn/titel/linkedin_url/sakerhet) är oförändrade; 'method' är nu alltid
    'claude_read' och 'källa' är URL:en till sidan som faktiskt lästes.
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return {"namn": "", "kalla": "ingen", "sakerhet": "låg", "method": "none",
                "källa": "", "linkedin_url": ""}

    # Open Brain: tidigare lärdomar kan styra vilken undersida som provas först.
    strategy = _recall_strategy(bransch, bolag)

    # 1) Hämta de mest sannolika sidorna — stanna när vi samlat nog.
    #    Startsidans menylänkar först (som David gör manuellt — löser sajter
    #    med språkprefix), sen de hårdkodade sökvägarna som komplement.
    pages: list[tuple[str, str]] = []
    fetches = 0
    base = _normalize_url(website)
    if base:
        home_html = _fetch_html(base)
        fetches += 1
        candidates = _discover_team_links(base, home_html)
        candidates += [f"{base}/{p}" if p else base for p in _order_paths(strategy)
                       if (f"{base}/{p}" if p else base) not in candidates]
        for url in candidates:
            if fetches >= MAX_FETCHES or len(pages) >= MAX_PAGES_TO_READ:
                break
            fetches += 1
            text = _fetch_page_text(url)
            if len(text) >= MIN_PAGE_CHARS:
                pages.append((url, text))

    # 2) Ingen undersida gav substans → web search får peka ut rätt sajt/sida.
    #    Viktigt när hemsida saknas på leadet och varumärket skiljer sig från
    #    juridiska namnet (Meson AB → mesongroup.com).
    discovered_site = ""
    if not pages:
        found_url = _search_contact_page(bolag)
        if found_url:
            p = urllib.parse.urlparse(found_url)
            root = f"{p.scheme}://{p.netloc}"
            if not base:
                discovered_site = root
            text = _fetch_page_text(found_url)
            if len(text) >= MIN_PAGE_CHARS:
                pages.append((found_url, text))
            # Sökningen kan ha gett startsidan — följ dess menylänkar också.
            if len(pages) < MAX_PAGES_TO_READ and fetches < MAX_FETCHES:
                fetches += 1
                home_html = _fetch_html(root)
                for url in _discover_team_links(root, home_html):
                    if fetches >= MAX_FETCHES or len(pages) >= MAX_PAGES_TO_READ:
                        break
                    if url.rstrip("/") == found_url.rstrip("/"):
                        continue
                    fetches += 1
                    t = _fetch_page_text(url)
                    if len(t) >= MIN_PAGE_CHARS:
                        pages.append((url, t))

    # OBS: Apify-rendering som reserv för IP-blockerade sajter provades men
    # stängdes av på Davids begäran — även nedsänkt till 90s tak kändes det
    # för segt i vardagen. Blockerade sajter (t.ex. ollesab.com) ger nu
    # "hittade ingen" snabbt; David klistrar in dem manuellt via ✏️ Kontakt.

    if not pages:
        _log(f"[people_finder] {bolag}: ingen läsbar sida hittades "
             f"({fetches} hämtningar) — troligen JS-renderad sajt eller fel domän")
        _remember_outcome(bolag, bransch, "", "")
        return {"namn": "", "titel": "", "linkedin_url": "", "kalla": "ingen",
                "källa": "", "website": discovered_site, "sakerhet": "låg",
                "method": "claude_read",
                "motivering": "Ingen läsbar sida hittades på bolagets hemsida."}

    # 3) Claude läser texten och pekar ut personen.
    data = _read_pages(bolag, bransch, target_role, pages, strategy)

    namn = str(data.get("namn", "")).strip()
    titel = str(data.get("titel", "")).strip() or target_role
    kalla_url = str(data.get("kalla_url", "")).strip() or pages[0][0]

    # Alla personer med namn + roll som lästes på sidan — David väljer i kortet.
    kandidater = []
    for k in (data.get("kandidater") or [])[:8]:
        if isinstance(k, dict) and str(k.get("namn", "")).strip():
            kandidater.append({
                "namn": str(k.get("namn", "")).strip(),
                "titel": str(k.get("titel", "")).strip(),
                "email": str(k.get("email", "")).strip(),
                "telefon": str(k.get("telefon", "")).strip(),
            })

    if not namn:
        _log(f"[people_finder] {bolag}: läste {[u for u, _ in pages]} → ingen person")
        _remember_outcome(bolag, bransch, pages[0][0], "")
        return {"namn": "", "titel": "", "linkedin_url": "", "kalla": "ingen",
                "källa": kalla_url, "website": discovered_site, "sakerhet": "låg",
                "method": "claude_read", "kandidater": kandidater,
                "motivering": str(data.get("motivering", "")).strip()
                or "Ingen person med relevant roll i hemsidetexten."}

    _log(f"[people_finder] {bolag}: läste {kalla_url} → {namn} ({titel})")
    _remember_outcome(bolag, bransch, kalla_url, namn, titel)

    return {
        "namn": namn,
        "titel": titel,
        # LinkedIn används inte längre som källa — fältet behålls (tomt) så
        # anropande kod inte går sönder.
        "linkedin_url": "",
        "kalla": "hemsida",
        "källa": kalla_url,
        "sakerhet": str(data.get("sakerhet", "")).strip() or "medel",
        "motivering": str(data.get("motivering", "")).strip(),
        "method": "claude_read",
        # Hemsida upptäckt via sökningen (när leadet saknade en) — spara på leadet.
        "website": discovered_site,
        # Personens egna kontaktuppgifter om de stod vid namnet i texten.
        "email": str(data.get("email", "")).strip(),
        "telefon": str(data.get("telefon", "")).strip(),
        # Alla lästa personer (namn/titel/mejl/tel) — för väljaren i leadskortet.
        "kandidater": kandidater,
    }
