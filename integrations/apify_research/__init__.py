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

── Paketstruktur (delades upp 2026-07-25 — var tidigare en enda 1041-radersfil,
den tredje mest ändrade filen i appen och samtidigt svårast att överblicka) ──

Resten av appen importerar fortfarande exakt som förut —
`from integrations import apify_research as apify` följt av t.ex.
`apify.find_companies(...)` eller `apify.find_emails(...)` — den här filen
(`__init__.py`) samlar bara ihop allt från undermodulerna nedan så att
INGENTING någon annanstans i koden behövde ändras.

  _maps.py         — prata med Apifys API: Google Maps-bolagssök, Google-sök,
                      LinkedIn-profilsök, och den delade "kör en actor"-motorn.
  _scrape.py        — läs text från en hemsida: startsida, team-/kontaktsidor,
                      sitemap.xml, schema.org-strukturdata. Ingen Apify alls,
                      bara vanlig HTTP.
  _contact.py       — hitta e-post + telefon på en hemsida, och gissa/verifiera
                      en hemsidas domän gratis (ingen Apify).
  _person_email.py  — gissa och SMTP-verifiera en NAMNGIVEN persons e-post.

Vill du ändra något specifikt: hemsidetext → _scrape.py, e-post/telefon på en
sajt → _contact.py, en persons mejladress → _person_email.py, prata med Apify
(Google Maps/Google-sök) → _maps.py.
"""

from ._contact import (
    _AT_OBFUSC,
    _CFEMAIL_LINK_RE,
    _CFEMAIL_RE,
    _DOT_OBFUSC,
    _EMAIL_JUNK,
    _EMAIL_LOWPRIO,
    _EMAIL_RE,
    _EMAIL_ROLE_PRIORITY,
    _GENERIC_WORDS,
    _LEGAL_TOKENS,
    _OBFUSC_EMAIL_RE,
    _PARKED_HINTS,
    _PHONE_TEXT_RE,
    _SOCIAL_DOMAINS,
    _SWE_MAP,
    _TEL_HREF_RE,
    _ascii_name,
    _clean_phone,
    _company_domain_stems,
    _crawl_rendered,
    _decode_cfemail,
    _extract_emails_from_html,
    _extract_phones,
    _page_matches_company,
    _probe,
    _rank_emails,
    find_company_website,
    find_emails,
    guess_company_website,
)
from ._maps import (
    APIFY_TOKEN,
    GOOGLE_ACTOR,
    MAPS_ACTOR,
    RENDER_ACTOR,
    RUN_TIMEOUT,
    _actor_path,
    _first,
    _normalize_place,
    _run_actor,
    clear_last_error,
    find_companies,
    find_linkedin_profiles,
    get_last_error,
    google_search,
    is_configured,
    remaining_usage_usd,
)
from ._person_email import (
    _GENERIC_LOCALS,
    _generate_email_variants,
    _infer_pattern,
    _mx_host,
    _smtp_verify,
    construct_person_email,
)
from ._scrape import (
    BeautifulSoup,
    _COMMON_CONTACT_PATHS,
    _HREF_RE,
    _HTML_RE,
    _TAG_RE,
    _TEAM_HINTS,
    _WS_RE,
    _get_html,
    _jsonld_people,
    _normalize_url,
    _sitemap_team_urls,
    _soup_text,
    _strip_html,
    _team_page_urls,
    fetch_people_pages,
    fetch_website_text,
)

__all__ = [
    "is_configured", "remaining_usage_usd", "find_companies", "google_search",
    "find_linkedin_profiles", "fetch_website_text", "fetch_people_pages",
    "find_emails", "guess_company_website", "find_company_website",
    "construct_person_email",
]
