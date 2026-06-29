"""
Allabolag — gratis ekonomisk data om svenska bolag (publik registerdata).

Hämtar ett bolags Allabolag-sida med vanlig HTTP och parsar den strukturerade
JSON-blobben (__NEXT_DATA__) som sidan redan innehåller. Ingen betald actor, ingen
LinkedIn, inget personkonto — datan kommer from Bolagsverket/Skatteverket via Allabolag.

Vi plockar ut precis det som behövs för IHA-screeningen:
  - nettoomsättning (kontokod SDI)
  - varulager        (kontokod SV)   ← kärnan: kapital bundet i lager
  - resultat         (resultat_e_finansnetto, fallback bolagets 'profit')
  - antal anställda
  - orgnr, namn, period

Beloppen i Allabolag är i TUSENTALS kronor (tkr). Vi exponerar dem i MSEK.

Allt är tåligt: hittas inget bolag eller saknas bokslut returneras {} och bolaget
hoppas bara över i screeningen.
"""

import re
import json
import time
import urllib.parse

from integrations import apify_research as apify

# Kontokoder i Allabolags bokslutsdata (verifierade mot riktiga sidor)
CODE_NETTOOMS = "SDI"          # Nettoomsättning
CODE_VARULAGER = "SV"          # Varulager
CODE_RESULTAT = "resultat_e_finansnetto"

_NEXT_RE = re.compile(r'__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.DOTALL)


def _fetch(url: str, tries: int = 4) -> str:
    """Hämta en Allabolag-sida med retry — söksidorna failar ibland transient."""
    for _ in range(tries):
        html = apify._get_html(url)
        if html and "__NEXT_DATA__" in html:
            return html
        time.sleep(1.5)
    return ""


def _to_float(val) -> float | None:
    """Tolka ett belopp ('18182', '-31.6', '1 234') som float. None om omöjligt."""
    if val in (None, ""):
        return None
    try:
        return float(str(val).replace(" ", "").replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _extract_next_data(html: str) -> dict:
    if not html:
        return {}
    m = _NEXT_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def find_company_url(company_name: str) -> str:
    """Hitta Allabolag-profil-URL för ett bolagsnamn via Google (liten kreditkostnad)."""
    company_name = (company_name or "").strip()
    if not company_name:
        return ""
    hits = apify.google_search(f"{company_name} allabolag.se", max_results=5)
    for h in hits:
        u = h.get("url", "")
        if "allabolag.se/foretag/" in u:
            return u
    return ""


def search_companies(bransch_term: str, ort: str = "", max_results: int = 25) -> list[dict]:
    """
    Sök bolag direkt på Allabolag (bransch + ort) — GRATIS, en hämtning ger ~25 aktiva
    bolag med omsättning, resultat och anställda redan ifyllt. Perfekt för förfilter
    innan vi hämtar detaljsidan (varulager) bara på de som är värda det.

    Returnerar normaliserade dicts: bolag, orgnr, omsattning_msek, resultat_msek,
    anstallda, website, bransch, ort.
    """
    bransch_term = (bransch_term or "").strip()
    if not bransch_term:
        return []
    ort = (ort or "").strip()
    if not ort or ort.lower() in ("hela sverige", "sverige"):
        ort = "Sverige"
    where = urllib.parse.quote(ort)
    what = urllib.parse.quote(bransch_term)
    url = f"https://www.allabolag.se/what/{what}/where/{where}"

    html = _fetch(url)
    data = _extract_next_data(html)
    try:
        # Struktur: ...searchStore.companies.companies (wrapper med hits/pages för paginering)
        companies = data["props"]["pageProps"]["hydrationData"]["searchStore"]["companies"]["companies"]
    except Exception:
        return []
    if not isinstance(companies, list):
        return []

    out = []
    for c in companies[:max_results]:
        if not isinstance(c, dict):
            continue
        rev = _to_float(c.get("revenue"))
        prof = _to_float(c.get("profit"))
        nace = c.get("naceIndustries") or []
        bransch = ""
        if isinstance(nace, list) and nace:
            first = nace[0]
            bransch = (first.get("name") if isinstance(first, dict) else str(first)) or ""
        loc = c.get("location") or {}
        emp = c.get("numberOfEmployees")
        try:
            emp = int(emp) if emp not in (None, "") else None
        except Exception:
            emp = None
        out.append({
            "bolag": str(c.get("name") or "").strip(),
            "orgnr": str(c.get("orgnr") or c.get("companyId") or "").strip(),
            "omsattning_msek": round(rev / 1000, 1) if rev else None,
            "resultat_msek": round(prof / 1000, 1) if prof is not None else None,
            "anstallda": emp,
            "website": str(c.get("homePage") or "").strip(),
            "bransch": str(bransch).strip(),
            "ort": str(loc.get("municipality") or loc.get("county") or "").strip(),
        })
    return out


def search_companies_multi(keywords: list[str], ort: str = "",
                           max_per_keyword: int = 25) -> list[dict]:
    """
    Poola flera sökord (t.ex. 'plasttillverkning', 'plastindustri', 'formsprutning') för
    samma ort och slå ihop till en unik lista (dedup på orgnr). Bredare nät → fler bolag
    i rätt storleksband. Allt gratis.
    """
    pooled: dict[str, dict] = {}
    for kw in keywords:
        for c in search_companies(kw, ort, max_results=max_per_keyword):
            key = c.get("orgnr") or c.get("bolag", "").lower()
            if key and key not in pooled:
                pooled[key] = c
    return list(pooled.values())


def segmentering(revenue_from_tkr: int, revenue_to_tkr: int, location: str = "",
                 profit_from_tkr: int | None = None, profit_to_tkr: int | None = None,
                 max_results: int = 40):
    """
    Allabolags Segmentering — SERVER-filtrerad bolagslista (gratis att läsa).
    Filtrerar på omsättning, rörelseresultat och plats direkt hos Allabolag och
    paginerar fram upp till max_results bolag. Belopp i TUSENtals kr (tkr).

    Returnerar normaliserade dicts: bolag, orgnr, omsattning_msek, resultat_msek,
    anstallda, website, bransch, ort. (Varulager finns inte här — hämtas per bolag
    via get_financials(orgnr) efteråt.)
    """
    base = (f"https://www.allabolag.se/segmentering?revenueFrom={int(revenue_from_tkr)}"
            f"&revenueTo={int(revenue_to_tkr)}")
    if location and not location.lower().startswith("hela sverige"):
        base += f"&location={urllib.parse.quote(location)}"
    if profit_from_tkr is not None:
        base += f"&profitFrom={int(profit_from_tkr)}"
    if profit_to_tkr is not None:
        base += f"&profitTo={int(profit_to_tkr)}"

    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while len(out) < max_results and page <= 50:
        data = _extract_next_data(_fetch(f"{base}&page={page}"))
        pp = data.get("props", {}).get("pageProps", {})
        companies = pp.get("companies") or []
        if not companies:
            break
        for c in companies:
            if not isinstance(c, dict):
                continue
            org = str(c.get("organisationNumber") or c.get("orgnr") or "").strip()
            key = org or str(c.get("name", "")).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rev = _to_float(c.get("revenue"))
            prof = _to_float(c.get("profit"))
            nace = c.get("naceCategories") or c.get("naceIndustries") or []
            bransch, nace_code = "", ""
            if isinstance(nace, list) and nace:
                first = nace[0]
                bransch = (first.get("name") if isinstance(first, dict) else str(first)) or ""
                m = re.match(r"\s*(\d+)", str(bransch))  # "16120 Bearbetning..." → "16120"
                if m:
                    nace_code = m.group(1)
            loc = c.get("location") or {}
            emp = c.get("numberOfEmployees")
            try:
                emp = int(emp) if emp not in (None, "") else None
            except Exception:
                emp = None
            out.append({
                "bolag": str(c.get("name") or "").strip(),
                "orgnr": org,
                "omsattning_msek": round(rev / 1000, 1) if rev else None,
                "resultat_msek": round(prof / 1000, 1) if prof is not None else None,
                "anstallda": emp,
                "website": str(c.get("homePage") or "").strip(),
                "bransch": str(bransch).strip(),
                "nace_code": nace_code,
                "ort": str(loc.get("municipality") or loc.get("county") or "").strip(),
            })
        pag = pp.get("pagination") or {}
        if not pag.get("next"):
            break
        page += 1
    return out[:max_results]


def segmentering_sweep(lan_list: list[str], revenue_from_tkr: int, revenue_to_tkr: int,
                       per_lan: int = 150, profit_from_tkr: int | None = None,
                       profit_to_tkr: int | None = None):
    """
    Generator: kör Segmentering (omsättning + plats, server-filtrerad SNI-data) ETT län i
    taget och yield:a (län, bolagslista). Varje län har sin egen pool, så att svepa alla 21
    län ger MÅNGDUBBELT fler bolag i rätt bransch än en enda nationell hämtning (där 150
    träffar delas av alla branscher). Anroparen poolar/dedupar och SNI-filtrerar.

    Låter UI visa progress per län. Allt gratis.
    """
    for lan in lan_list:
        yield lan, segmentering(revenue_from_tkr, revenue_to_tkr, location=lan,
                                profit_from_tkr=profit_from_tkr, profit_to_tkr=profit_to_tkr,
                                max_results=per_lan)


def search_lan_sweep(keywords: list[str], lan_list: list[str]):
    """
    Generator: svep en bransch över flera län och yield:a (län, bolagslista) i taget.
    Låter UI visa progress. Poolar inte mellan län — anroparen slår ihop.

    Obs: sökord-varianter utökar inte poolen inom ETT län (Allabolag ger samma ~25),
    det är geografin som breddar. Därför används bara första sökordet per län — det
    håller antalet hämtningar nere (ett per län) utan att tappa träffar.
    """
    primary = keywords[0] if keywords else ""
    for lan in lan_list:
        yield lan, search_companies(primary, lan, max_results=25)


def parse_company(html: str) -> dict:
    """Parsa en hämtad Allabolag-sida till strukturerad ekonomi. {} om data saknas."""
    data = _extract_next_data(html)
    try:
        company = data["props"]["pageProps"]["company"]
    except Exception:
        return {}
    if not isinstance(company, dict):
        return {}

    accounts_list = company.get("companyAccounts") or []
    accounts: dict[str, str] = {}
    period = ""
    if accounts_list:
        latest = accounts_list[0]
        period = str(latest.get("year") or latest.get("period") or "")
        accounts = {r.get("code"): r.get("amount")
                    for r in (latest.get("accounts") or []) if isinstance(r, dict)}

    nettooms_tkr = _to_float(accounts.get(CODE_NETTOOMS))
    if nettooms_tkr is None:
        nettooms_tkr = _to_float(company.get("revenue"))
    varulager_tkr = _to_float(accounts.get(CODE_VARULAGER)) or 0.0
    resultat_tkr = _to_float(accounts.get(CODE_RESULTAT))
    if resultat_tkr is None:
        resultat_tkr = _to_float(company.get("profit"))

    anstallda = company.get("numberOfEmployees")
    try:
        anstallda = int(anstallda) if anstallda not in (None, "") else None
    except Exception:
        anstallda = None

    industries = company.get("industries") or company.get("naceIndustries") or []
    bransch = ""
    if isinstance(industries, list) and industries:
        first = industries[0]
        bransch = (first.get("name") if isinstance(first, dict) else str(first)) or ""

    return {
        "bolag": str(company.get("name") or company.get("legalName") or "").strip(),
        "orgnr": str(company.get("orgnr") or "").strip(),
        "website": str(company.get("homePage") or "").strip(),
        "bransch": str(bransch).strip(),
        "ort": str(company.get("domicile") or "").strip(),
        "period": period,
        "omsattning_msek": round(nettooms_tkr / 1000, 1) if nettooms_tkr else None,
        "varulager_msek": round(varulager_tkr / 1000, 1) if varulager_tkr else 0.0,
        "resultat_msek": round(resultat_tkr / 1000, 1) if resultat_tkr is not None else None,
        "anstallda": anstallda,
        "_nettooms_tkr": nettooms_tkr,
        "_varulager_tkr": varulager_tkr,
        "_resultat_tkr": resultat_tkr,
    }


def get_financials(company_name: str = "", url: str = "", orgnr: str = "") -> dict:
    """
    Hämta och parsa full ekonomi (inkl. varulager) för ETT bolag. Ange orgnr (gratis,
    direkt — rekommenderas), en färdig URL, eller ett namn (slås upp via Google).
    Returnerar parse_company-dict + 'url'. {} om inget hittas.
    """
    if not url:
        if orgnr:
            url = f"https://www.allabolag.se/{orgnr.strip()}"
        elif company_name:
            url = find_company_url(company_name)
    if not url:
        return {}
    fin = parse_company(_fetch(url))
    if fin:
        fin["url"] = url
    return fin
