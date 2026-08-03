"""
E-post + telefon (gratis, publik scraping, ingen LinkedIn) + gratis
hemsidegissning. Hittar publika kontaktuppgifter på ett bolags hemsida, och
kan gissa/verifiera en hemsidas domän även utan Apify.

Del av `integrations/apify_research` — se paketets `__init__.py`.
"""

import re
import unicodedata
import urllib.parse

import requests

from ._maps import RENDER_ACTOR, _run_actor, google_search, is_configured
from ._scrape import _COMMON_CONTACT_PATHS, _get_html, _normalize_url, _team_page_urls

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

# Konverterar svenska tecken till ASCII för domän-/e-postgissning.
_SWE_MAP = str.maketrans("åäöüéèêàÅÄÖÜÉÈÊÀ", "aaoueeeaAAOUEEEA")


def _ascii_name(s: str) -> str:
    """'Karin Söderqvist' → 'karin soderqvist' (ASCII, lowercase)."""
    s = s.lower().translate(_SWE_MAP)
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


def _generate_email_variants(namn: str, domain: str) -> list[str]:
    """
    Generera troliga e-postadresser för en person på given domän.
    Täcker de vanligaste svenska namnmönstren.

    Ligger här (flyttad från _person_email.py 2026-08-03, som togs bort):
    modulen innehöll construct_person_email() med SMTP-verifiering, men den
    anropades aldrig från någonstans i appen. Den här funktionen var det enda
    som faktiskt användes (av agents/people_finder.py) och hör ändå ihop med
    _ascii_name här intill.
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
    """
    True om sidan rimligt hör till bolaget (och inte är en parkerad domän).

    Kräver att namnorden dyker upp som en SAMMANHÄNGANDE FRAS ("side system"),
    inte bara att varje ord för sig råkar finnas nånstans på sidan. Att bara
    kräva att alla ord matchar var för sig (tidigare fix, för Schuchardt
    Maskin AB) räcker inte när namnet består av vanliga engelska ord — "Side
    System AB" gissades till side.com, en stor amerikansk mäklarplattform där
    både "side" OCH "system" råkar finnas naturligt i löptexten (och till och
    med en "recruiting@side.com" som felaktigt sparades som VD:ns mejl).
    Enordsnamn (t.ex. "Vimek") matchas som helt ord (\\b), inte substräng,
    så "vimek" inte råkar träffa en oturlig delsträng i annan text.
    """
    low = html.lower()
    if any(bad in low for bad in _PARKED_HINTS):
        return False
    tokens = [w for w in re.split(r"[^a-z0-9]+", _ascii_name(bolag))
              if len(w) >= 3 and w not in _LEGAL_TOKENS]
    if not tokens:
        return True
    if len(tokens) == 1:
        return re.search(rf"\b{re.escape(tokens[0])}\b", low) is not None
    phrase = " ".join(tokens[:3])
    return phrase in low


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
