"""
E-postskrivare v2 — rollanpassade säljmejl med Hormozi-struktur.

Tre spår baserade på mottagarens titel:
  vd      → helhetsrisk, kassaflöde, bolagets konkurrenskraft
  cfo     → DOS (days of stock), kapitalbindning i kr, balansräkningseffekt
  scm     → operativ igenkänning, rotorsaker, okänd dödlager-skuld
  neutral → CFO-lutad men rolloberoende (fallback om titel saknas/oklar)

Confidence-nivåer (styr om mailet flaggas för granskning):
  high   → bolagsspecifik data + nyheter ELLER känd titel → skickas direkt
  medium → bolagsspecifik data men ingen nyhetsresearch → skickas direkt
  low    → saknar nyckeltal → flaggas för manuell granskning (review_flag=True)

Proof point som alltid inkluderas som "Perceived Likelihood":
  18+ MSEK dödlager identifierat på 5 dagar hos nordiskt industribolag (~1 800 SKU:er).
"""

import json
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-4-8"

SIGNATURE = (
    "Vänliga hälsningar,\n"
    "David Leifsson\n"
    "Logistics Doctor\n"
    "Tel: 0737168367\n"
    "LinkedIn: https://www.linkedin.com/in/davidleifsson/\n"
    "www.barisab.com"
)

PROOF_POINT = (
    "Hos ett nordiskt industribolag identifierade vi 18+ MSEK i dödlager på 5 dagar "
    "— ca 1 800 SKU:er. Ingen ERP-integration, ingen IT-avdelning inblandad."
)

# Riskreversering — Hormozis starkaste spak för "perceived likelihood".
GUARANTEE = (
    "5× ROI-garanti: frigör analysen inte minst 5 gånger sitt pris i kapital "
    "betalar du ingenting."
)

# ── Rolligenkänning ────────────────────────────────────────────────────────────

_VD_KEYS  = ["vd", "ceo", "verksamhetschef", "managing director", "general manager",
              "ägare", "owner", "grundare", "founder", "president"]
_CFO_KEYS = ["cfo", "ekonomichef", "finanschef", "controller", "finans", "redovisning",
              "finance manager", "chief financial"]
_SCM_KEYS = ["inköp", "supply chain", "sc manager", "logistik", "lager", "warehouse",
              "operations", "purchasing", "procurement", "materialplanerare"]


def _detect_role(titel: str) -> str:
    """Returnerar 'vd' | 'cfo' | 'scm' | 'neutral'."""
    t = (titel or "").lower()
    if any(k in t for k in _VD_KEYS):
        return "vd"
    if any(k in t for k in _CFO_KEYS):
        return "cfo"
    if any(k in t for k in _SCM_KEYS):
        return "scm"
    return "neutral"


def _first_name(namn: str) -> str:
    namn = (namn or "").strip()
    return namn.split()[0] if namn else ""


def _dos(varulager_msek, omsattning_msek) -> int | None:
    """Days of Stock = varulager / (omsattning / 365)."""
    try:
        v, o = float(varulager_msek), float(omsattning_msek)
        if v > 0 and o > 0:
            return round(v * 365 / o)
    except Exception:
        pass
    return None


def _freeable_range(varulager_msek) -> tuple[float, float] | None:
    """
    Grovt drömresultat i MSEK: erfarenhetsmässigt sitter 15–30 % av lagervärdet
    i döda/långsamma artiklar i lager-tunga bolag med låg omsättningshastighet.
    Presenteras ALLTID som estimat i mejlet, aldrig som fastställd siffra.
    """
    try:
        v = float(varulager_msek)
        if v > 0:
            return round(v * 0.15, 1), round(v * 0.30, 1)
    except Exception:
        pass
    return None


def _confidence(titel: str, lagerandel, varulager_msek, nyheter: str) -> tuple[str, bool]:
    """Returnerar (confidence_level, review_flag)."""
    has_data = (lagerandel is not None) and (varulager_msek is not None)
    has_trigger = bool(nyheter and nyheter.strip()) or _detect_role(titel) != "neutral"
    if has_data and has_trigger:
        return "high", False
    if has_data:
        return "medium", False
    return "low", True


# ── Rollspecifika systemprompt-block ──────────────────────────────────────────

_BASE_RULES = f"""
SÅ HÄR SKRIVER DU (icke förhandlingsbart):

1) SPECIFICITET SÄLJER — detta är det viktigaste.
Varje mening ska kunna gälla ENBART detta bolag. Ett mejl som lika gärna kunde
gått till vilket bolag som helst är ett misslyckat mejl.
- Öppna med bolagets EGEN siffra/signal: varulager i MSEK, Days of Stock vs
  branschnorm, lagerandel, eller en konkret nyhet du fått. Aldrig "jag såg att ni
  är verksamma inom..." eller en allmän branschobservation.
- Exakta tal, inte runda ("~{{dos}} dagars lager" slår "mycket lager"; "43 MSEK"
  slår "stora summor").
- FÖRBJUDNA generiska fraser — använd dem ALDRIG: "jag hoppas mailet finner dig
  väl", "vi hjälper företag att", "effektivisera er verksamhet", "ta verksamheten
  till nästa nivå", "jag ville bara höra av mig", "spännande möjlighet", "win-win",
  "tveka inte att höra av dig".

2) VÄRDEEKVATIONEN (nämn den ALDRIG vid namn i mejlet).
Värde = (Drömresultat × Sannolikhet att lyckas) / (Tid × Ansträngning).
Maximera täljaren, minimera nämnaren:
- DRÖMRESULTAT i kronor: frigjort kapital + den lagerhållningskostnad/år som
  försvinner. Rama in KOSTNADEN AV ATT INTE AGERA — pengarna kostar varje månad de
  står kvar i hyllan. Använd det uppskattade frigörbara spannet du fått, tydligt
  som estimat ("erfarenhetsmässigt sitter 15–30 % av lagervärdet...").
- SANNOLIKHET: proof point + att slutsatsen bygger på ERA EGNA bokslutssiffror
  (inte gissningar). Du FÅR väva in riskreverseringen som förtroendesignal:
  "{GUARANTEE}"
- TID: konkret ("en första bild inom en vecka, färdig analys på två veckor").
- ANSTRÄNGNING: nära noll för dem — "en export ur ert affärssystem, vi gör resten.
  Ingen IT, inga möten, inget nytt system."

3) ERBJUDANDET ÄR SAMTALET, INTE ANALYSEN. Sälj ett kort samtal och gör det riskfritt.
- Ett enda tydligt CTA: 15 minuter.
- Avriskera själva samtalet: t.ex. "Hittar vi inget värt att agera på har du
  förlorat 15 minuter — och du får ramverket att köra själv ändå." Hitta inte på
  andra löften.

TON & FORM:
- Svenska (engelska om bolaget är tydligt internationellt). Du-tilltal, mänskligt,
  noll säljhype, inga utropstecken.
- KORT: 5–7 meningar i brödtexten. Läsbart på mobil.
- Hitta ALDRIG på siffror, händelser eller resultat. Använd bara det du fått.
  Estimat ramas ALLTID in som estimat.
- Öppningsraden = bolagets signal/siffra. Vem David är kommer först i mening 2–3,
  kort (Logistics Doctor, hittar bundet kapital i lager).
- Avsluta ALLTID med exakt denna signatur:
{SIGNATURE}

Returnera EXAKT detta JSON (inget utanför):
{{"subject": "...", "body": "hela mejltexten inkl. hälsning och signatur"}}
Ämnesraden: max 8 ord, MED en konkret siffra eller spänning (t.ex.
"~X MSEK står stilla hos {{bolag}}"). Ingen clickbait, inga versaler."""


_ROLE_INSTRUCTIONS = {
    "vd": f"""Mottagare: VD, ägare eller grundare.
VINKEL: helhetsrisk och kassaflöde — inte operativa detaljer. Kapital bundet i
lager är kapital som inte kan gå till tillväxt, amortering eller utdelning.
DRÖMRESULTAT att lyfta: frigjort kassaflöde och handlingsfrihet, uttryckt i kronor
(frigörbart kapital + årlig lagerhållningskostnad som försvinner). Riskreverseringen
(5× ROI-garantin) passar bra här som förtroendesignal.
{_BASE_RULES}""",

    "cfo": f"""Mottagare: CFO, ekonomichef eller controller.
VINKEL: balansräkning och siffror. Använd Days of Stock och lagerandel mot
branschnorm för att visa hur mycket kapital som ligger ÖVER en sund nivå.
DRÖMRESULTAT att lyfta: sänkt DOS och frigjort kapital som syns direkt i
balansräkningen, plus borttagen lagerhållningskostnad/år. Riskreverseringen
(5× ROI-garantin) hör hemma här — en CFO värderar avriskering.
{_BASE_RULES}""",

    "scm": f"""Mottagare: inköpschef, supply chain manager eller logistikchef.
VINKEL: operativ igenkänning FÖRST, inte kapitalbindning som första krok. Säg det
ingen säger högt: lager som vuxit av sig självt, parametrar ingen rört på år,
kunskap som försvann när någon slutade, artiklar ingen vågar skrota.
DRÖMRESULTAT att lyfta: äntligen veta VILKA artiklar som är döda, VARFÖR de sitter
där och VAD man gör åt det — SKU för SKU. Kronorna får komma sekundärt.
{_BASE_RULES}""",

    "neutral": f"""Mottagare: okänd person eller allmän/info-adress.
VINKEL: luta mot CFO-spåret (siffror) men håll det rolloberoende. Eftersom du inte
vet vem som läser: gör drömresultatet tydligt i kronor och håll språket enkelt.
DRÖMRESULTAT att lyfta: identifiera och frigöra det kapital som ligger bundet i
lager, uttryckt konkret. Riskreverseringen (5× ROI-garantin) får vara med.
{_BASE_RULES}""",
}


# ── Nyhetsresearch (valfritt, kräver Apify) ───────────────────────────────────

def fetch_company_context(bolag: str, bransch: str = "") -> str:
    """
    Sök nyheter och triggers för bolaget via Google (Apify).
    Returnerar kortfattad text med relevanta snippets, eller "" om Apify
    ej är konfigurerat eller inget hittas. Aldrig påhittat innehåll.
    """
    try:
        from integrations import apify_research as _apify
        if not _apify.is_configured():
            return ""
        query = f'"{bolag}" nyheter OR expansion OR VD OR pressrelease'
        results = _apify.google_search(query, max_results=5, country="se", language="sv")
        if not results:
            return ""
        snippets = []
        for r in results[:3]:
            title = (r.get("title") or "").strip()
            desc = (r.get("description") or "").strip()[:200]
            if title:
                snippets.append(f"- {title}{(': ' + desc) if desc else ''}")
        return "\n".join(snippets)
    except Exception:
        return ""


# ── Företagsunik personalisering (Hormozi: 1–3 specifika fakta öppnar dörren) ──

def _financial_trends(history: list[dict] | None) -> list[str]:
    """
    Gör flerårshistoriken (ur Bolagsverkets årsredovisningar via Allabolag) till
    konkreta, verifierbara krokar. Starkaste IHA-signalen (lager upp + omsättning
    ner) läggs först. Tom lista om historik saknas.
    """
    rows = [h for h in (history or [])
            if h.get("omsattning_msek") or h.get("varulager_msek")]
    if len(rows) < 2:
        return []
    new, old = rows[0], rows[-1]                 # nyast först i listan
    yn, yo = new.get("år", ""), old.get("år", "")
    vn, vo = new.get("varulager_msek"), old.get("varulager_msek")
    on, oo = new.get("omsattning_msek"), old.get("omsattning_msek")
    rn = new.get("resultat_msek")
    facts: list[str] = []

    # 1. Varulagertillväxt
    if vn and vo and vo > 0:
        pct = round((vn - vo) / vo * 100)
        if pct >= 15:
            facts.append(f"Varulagret växte {pct}% ({vo}→{vn} MSEK) mellan {yo} och {yn}.")
    # 2. Divergens — lager upp MEDAN omsättning ner (lägg först, starkast)
    if vn and vo and on and oo and vn > vo and on < oo:
        facts.insert(0, f"Lagret växer medan försäljningen faller: varulager {vo}→{vn} MSEK "
                        f"men omsättning {oo}→{on} MSEK ({yo}→{yn}) — klassiskt tecken på "
                        f"kapital som fastnar i hyllan.")
    # 3. Lagerandel-förändring
    if vn and on and vo and oo and on > 0 and oo > 0:
        la_new, la_old = round(vn / on * 100), round(vo / oo * 100)
        if la_new - la_old >= 5:
            facts.append(f"Lagerandelen steg från {la_old}% till {la_new}% av omsättningen "
                         f"({yo}→{yn}).")
    # 4. Resultatsving (från vinst till svagare/förlust)
    results = [(h.get("år"), h.get("resultat_msek")) for h in rows
               if h.get("resultat_msek") is not None]
    if rn is not None and results:
        best_y, best_r = max(results, key=lambda t: t[1])
        if best_r - rn >= 5 and best_r > 0:
            facts.append(f"Resultatet vände från +{best_r} MSEK ({best_y}) till "
                         f"{'+' if rn >= 0 else ''}{rn} MSEK ({yn}).")
    return facts


def _company_profile(website: str) -> str:
    """Kort text om vad bolaget faktiskt gör, från deras hemsida (gratis). '' vid fel."""
    if not website:
        return ""
    try:
        from integrations import apify_research as _apify
        return _apify.fetch_website_text(website, max_chars=1200).strip()
    except Exception:
        return ""


# ── Huvudfunktion ──────────────────────────────────────────────────────────────

def generate_email(
    bolag: str,
    namn: str = "",
    titel: str = "",
    bransch: str = "",
    lagerandel=None,
    varulager_msek=None,
    omsattning_msek=None,
    nyheter: str = "",
    language: str = "sv",
    orgnr: str = "",
    website: str = "",
    history=None,
    foretagsinfo: str = "",
) -> dict:
    """
    Generera ett rollanpassat, FÖRETAGSUNIKT säljmejl.

    Berikar automatiskt med flerårshistorik (Bolagsverkets årsredovisningar via
    Allabolag, om orgnr ges) och vad bolaget gör (deras hemsida, om website ges)
    → 1–3 verifierbara krokar som öppnar mejlet (Hormozi-personalisering).

    Returnerar:
      {subject, body, roll_spår, confidence, review_flag}
    """
    roll = _detect_role(titel)

    # Berika: flerårstrend + företagsprofil → företagsunika krokar.
    if history is None and orgnr:
        try:
            from integrations import allabolag as _ab
            _fin = _ab.get_financials(orgnr=orgnr)
            history = _fin.get("history") if _fin else None
        except Exception:
            history = None
    trends = _financial_trends(history)
    if not foretagsinfo and website:
        foretagsinfo = _company_profile(website)

    confidence, review_flag = _confidence(titel, lagerandel, varulager_msek, nyheter)
    # En flerårstrend är stark, verifierbar personalisering → hög confidence.
    if trends:
        confidence, review_flag = "high", False
    dos = _dos(varulager_msek, omsattning_msek)

    # ── Faktablock ────────────────────────────────────────────────────────────
    fakta = [f"Bolag: {bolag}"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if omsattning_msek is not None:
        fakta.append(f"Omsättning: {omsattning_msek} MSEK")
    if varulager_msek is not None:
        fakta.append(f"Varulager: {varulager_msek} MSEK")
        try:
            hold_cost = round(float(varulager_msek) * 0.20, 1)
            fakta.append(f"Lagerhållningskostnad/år på HELA lagret (~20%): {hold_cost} MSEK "
                         f"(kontext — attributera INTE denna till bara den döda delen)")
        except Exception:
            pass
        fr = _freeable_range(varulager_msek)
        if fr:
            waste_lo = round(fr[0] * 0.20, 1)
            waste_hi = round(fr[1] * 0.20, 1)
            fakta.append(
                f"DRÖMRESULTAT att rama in (ESTIMAT — presentera som spann, ej fastställt): "
                f"~{fr[0]}–{fr[1]} MSEK frigörbart kapital "
                f"(erfarenhetsmässigt 15–30 % av lagervärdet sitter i döda/långsamma artiklar)."
            )
            fakta.append(
                f"KOSTNAD AV ATT INTE AGERA (använd denna, korrekt attribuerad): just den "
                f"döda/långsamma delen kostar ~{waste_lo}–{waste_hi} MSEK/år att lagra "
                f"(~20% av det frigörbara spannet) — pengar som brinner varje år den står kvar."
            )
    if lagerandel is not None:
        fakta.append(f"Lagerandel (varulager/oms): {lagerandel}%")
    if dos is not None:
        fakta.append(f"Days of Stock (DOS): ~{dos} dagar")
        # Branschnormer för kontextualisering
        if bransch:
            bl = bransch.lower()
            if any(k in bl for k in ["tillverk", "manufactur", "industri"]):
                fakta.append("Branschnorm DOS tillverkning: 60–90 dagar")
            elif any(k in bl for k in ["grossist", "wholesale", "handel"]):
                fakta.append("Branschnorm DOS grossist: 30–45 dagar")

    # ── Mottagare ─────────────────────────────────────────────────────────────
    fornamn = _first_name(namn)
    if fornamn:
        halsning = f"Hej {fornamn},"
        mottagare = f"Mottagare: {namn} ({titel or 'okänd titel'})"
    else:
        halsning = "Hej,"
        mottagare = f"Mottagare: ingen känd person — använd neutral hälsning 'Hej,' utan namn."

    # ── Nyheter/triggers ──────────────────────────────────────────────────────
    nyhets_block = ""
    if nyheter and nyheter.strip():
        nyhets_block = (
            f"\nBolagsspecifika nyheter/triggers (använd den mest relevanta som öppning):\n"
            f"{nyheter.strip()}\n"
        )
    else:
        nyhets_block = "\n(Inga bolagsspecifika nyheter hittades — använd siffrorna som trigger.)\n"

    # ── Företagsunika krokar + profil ─────────────────────────────────────────
    if trends:
        krok_block = (
            "\nFÖRETAGSUNIKA KROKAR ur deras årsredovisningar (Bolagsverket, flera år) "
            "— ÖPPNA mejlet med 1–2 av dessa, ordagrant och verifierbart. Detta är det "
            "viktigaste i hela mejlet:\n"
            + "\n".join(f"  - {t}" for t in trends) + "\n")
    else:
        krok_block = ("\n(Ingen flerårstrend tillgänglig — öppna med senaste årets "
                      "lagerandel/DOS som företagsspecifik krok.)\n")
    profil_block = ""
    if foretagsinfo:
        profil_block = (
            "\nOM BOLAGET (text från deras hemsida — referera KONKRET vad de "
            "tillverkar/säljer så det märks att mejlet är skrivet till just dem; "
            "hitta inte på):\n" + foretagsinfo[:1000] + "\n")

    # ── Bygg user-prompt ──────────────────────────────────────────────────────
    user_msg = (
        f"Skriv ett rollanpassat, FÖRETAGSUNIKT kall-mejl baserat på fakta nedan.\n\n"
        f"FAKTA OM BOLAGET (använd, hitta inte på mer):\n"
        + "\n".join(f"  {f}" for f in fakta)
        + f"\n{krok_block}{profil_block}"
        + f"\n{mottagare}\n"
        f"Hälsning att använda: {halsning}\n"
        f"{nyhets_block}\n"
        f"ROLLSPÅR: {roll.upper()} — följ instruktionerna för detta spår exakt.\n"
        f"KRAV: Första meningen ska vara en företagsunik iakttagelse om JUST detta "
        f"bolag — helst en flerårstrend ur årsredovisningen (t.ex. att lagret vuxit "
        f"medan omsättningen fallit). Väv gärna in vad de tillverkar. ALDRIG en "
        f"generisk branschmening som kunde gått till vilket bolag som helst.\n"
        f"Returnera JSON."
    )

    if language == "en":
        user_msg += "\nOBS: Skriv mejlet på ENGELSKA (bolaget verkar internationellt)."

    # ── Anropa Claude ──────────────────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=_ROLE_INSTRUCTIONS[roll],
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()

    # Extrahera JSON
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    body = str(data.get("body", "")).strip()
    # Säkerhetsnet: garantera signatur
    if "0737168367" not in body or "linkedin.com/in/davidleifsson" not in body.lower():
        body = body.rstrip() + "\n\n" + SIGNATURE

    return {
        "subject": str(data.get("subject", "")).strip() or f"{bolag} – {varulager_msek or '?'} MSEK i lager",
        "body": body,
        "roll_spår": roll,
        "confidence": confidence,
        "review_flag": review_flag,
    }
