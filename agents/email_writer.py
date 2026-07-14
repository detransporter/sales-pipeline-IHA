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

# Fable 5 efter A/B-test 2026-07-08 (samma beslut som IHA:s outreach-roll):
# vassare hook, bättre regelefterlevnad, mänskligare ton. ~1,50 kr/mejl.
MODEL = "claude-sonnet-5"

SIGNATURE = (
    "Vänliga hälsningar,\n"
    "David Leifsson\n"
    "Logistics Doctor\n"
    "david.leifsson@barisab.com\n"
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

2) MAX 3–4 SIFFROR TOTALT I HELA MEJLET.
Faktablocket du får är kontext för DIG — inte en checklista att tömma i mejlet.
Välj de 3–4 mest slagkraftiga siffrorna (helst en flerårstrend + ETT kronbelopp)
och lämna resten. Fler siffror än så känns som en rapport, inte ett mejl.
- Ett spann räknas som EN siffra (t.ex. "12–23 MSEK"). Årtal räknas inte.
- Verbalisera hellre än sifferstapla: "över halva omsättningen" slår "58 %",
  "lagret växte snabbare än försäljningen" kan ersätta ett procenttal.

3) VÄRDEEKVATIONEN (nämn den ALDRIG vid namn i mejlet).
Värde = (Drömresultat × Sannolikhet att lyckas) / (Tid × Ansträngning).
Maximera täljaren, minimera nämnaren — men fördela över SEPARATA stycken (se
struktur nedan), inte i en enda mening:
- DRÖMRESULTAT i kronor: frigjort kapital ELLER lagerhållningskostnaden/år som
  försvinner — välj EN av dem, inte båda, om siffertaket redan är nått.
  Rama in som estimat ("erfarenhetsmässigt sitter 15–30 % av lagervärdet...").
- SANNOLIKHET: proof point + att slutsatsen bygger på ERA EGNA bokslutssiffror
  (inte gissningar).
- TID: konkret ("en första bild inom en vecka, färdig analys på två veckor").
- ANSTRÄNGNING: nära noll för dem — "en export ur ert affärssystem, vi gör resten.
  Ingen IT, inga möten, inget nytt system."

4) GARANTIN FÅR ETT EGET STYCKE.
Riskreverseringen ("{GUARANTEE}") ska INTE vävas in i samma mening som proof
point eller drömresultat. Den står ensam, kort, som ett eget stycke — det gör
den till en tydlig signal istället för "ännu en siffra bland andra".

5) STRUKTUR — 5–6 STYCKEN, SEPARERADE MED BLANKRAD (\\n\\n i JSON-fältet).
Aldrig ett sammanhängande textblock. Varje stycke = EN tanke:
   (1) Hook — bolagets egen siffra/signal (1 mening)
   (2) Vem David är — kort (1 mening)
   (3) Drömresultat — EN beräkning/spann
   (4) Hur det går till — vad du behöver + tidslinje + låg ansträngning
   (5) Garanti — eget stycke
   (6) CTA — en fråga

6) ERBJUDANDET ÄR SAMTALET, INTE ANALYSEN. Sälj ett kort samtal och gör det riskfritt.
- Ett enda tydligt CTA i sista stycket: 15 minuter.
- Avriskera samtalet i SAMMA stycke som CTA:n, inte som en konkurrerande fråga:
  t.ex. "Har du 15 minuter — hittar vi inget värt att agera på har du bara
  förlorat en kvart, och du får ramverket att köra själv ändå."

TON & FORM:
- Svenska (engelska om bolaget är tydligt internationellt). Du-tilltal, mänskligt,
  noll säljhype, inga utropstecken.
- ALDRIG ord i VERSALER i mejltexten (skriv "vilka artiklar som är döda", aldrig
  "VILKA artiklar") — versaler läses som att man skriker.
- KORT: 5–6 stycken enligt strukturen ovan, 1–2 meningar per stycke.
  Läsbart på mobil utan mer än en scroll.
- Hitta ALDRIG på siffror, händelser eller resultat. Använd bara det du fått.
  Estimat ramas ALLTID in som estimat.
- Öppningsraden = bolagets signal/siffra. Vem David är kommer i stycke 2, kort
  (Logistics Doctor, hittar bundet kapital i lager).
- Avsluta ALLTID med exakt denna signatur:
{SIGNATURE}

Returnera EXAKT detta JSON (inget utanför):
{{"subject": "...", "body": "hela mejltexten inkl. hälsning och signatur, med
blankrad (\\n\\n) mellan varje stycke enligt strukturen ovan"}}
Ämnesraden: 8–10 ord, MED en konkret siffra eller spänning (t.ex.
"~X MSEK står stilla hos {{bolag}}"). Ingen clickbait, inga versaler."""


_ROLE_INSTRUCTIONS = {
    "vd": f"""Mottagare: VD, ägare eller grundare.
VINKEL: helhetsrisk och kassaflöde — inte operativa detaljer. Kapital bundet i
lager är kapital som inte kan gå till tillväxt, amortering eller utdelning.
DRÖMRESULTAT att lyfta: frigjort kassaflöde och handlingsfrihet, uttryckt i kronor.
Garantin står som eget stycke enligt regel 4.
{_BASE_RULES}""",

    "cfo": f"""Mottagare: CFO, ekonomichef eller controller.
VINKEL: balansräkning och siffror. Använd Days of Stock eller lagerandel mot
branschnorm för att visa hur mycket kapital som ligger över en sund nivå.
DRÖMRESULTAT att lyfta: frigjort kapital som syns direkt i balansräkningen.
Garantin står som eget stycke enligt regel 4 — en CFO värderar avriskering.
{_BASE_RULES}""",

    "scm": f"""Mottagare: inköpschef, supply chain manager eller logistikchef.
VINKEL: operativ igenkänning FÖRST, inte kapitalbindning som första krok. Säg det
ingen säger högt: lager som vuxit av sig självt, parametrar ingen rört på år,
kunskap som försvann när någon slutade, artiklar ingen vågar skrota.
DRÖMRESULTAT att lyfta: att äntligen veta vilka artiklar som är döda, varför de
sitter där och vad man gör åt det — SKU för SKU (skriv det med små bokstäver i
mejlet, aldrig versaler). Kronorna får komma sekundärt.
{_BASE_RULES}""",

    "neutral": f"""Mottagare: okänd person eller allmän/info-adress.
VINKEL: luta mot CFO-spåret (siffror) men håll det rolloberoende. Eftersom du inte
vet vem som läser: gör drömresultatet tydligt i kronor och håll språket enkelt.
DRÖMRESULTAT att lyfta: identifiera och frigöra det kapital som ligger bundet i
lager, uttryckt konkret. Garantin står som eget stycke enligt regel 4.
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
    resultat_msek=None,
    nyheter: str = "",
    language: str = "sv",
    orgnr: str = "",
    website: str = "",
    history=None,
    foretagsinfo: str = "",
    followup_steg: int = 0,
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

    # Berika: hämta bokslut (historik + bruttomarginal + aktuella tal) via orgnr.
    bruttomarginal = None
    if orgnr:
        try:
            from integrations import allabolag as _ab
            _fin = _ab.get_financials(orgnr=orgnr)
            if _fin:
                if history is None:
                    history = _fin.get("history")
                bruttomarginal = _fin.get("bruttomarginal")
                # Backfilla saknade aktuella tal ur bokslutet.
                if omsattning_msek is None:
                    omsattning_msek = _fin.get("omsattning_msek")
                if varulager_msek is None:
                    varulager_msek = _fin.get("varulager_msek")
                if resultat_msek is None:
                    resultat_msek = _fin.get("resultat_msek")
        except Exception:
            pass

    # Deterministisk KPI-motor → exakt samma siffror som IHA-analysen.
    from agents import iha_metrics
    metrics = iha_metrics.compute(
        bolag=bolag, bransch=bransch, omsattning_msek=omsattning_msek,
        varulager_msek=varulager_msek, resultat_msek=resultat_msek,
        bruttomarginal=bruttomarginal, lagerandel=lagerandel, history=history)
    kpi = metrics.get("kpi", {})
    insights = metrics.get("insights", [])
    headline = metrics.get("headline", "")

    if not foretagsinfo and website:
        foretagsinfo = _company_profile(website)

    confidence, review_flag = _confidence(titel, lagerandel, varulager_msek, nyheter)
    # Förberäknade, verifierbara krokar → hög confidence.
    if insights:
        confidence, review_flag = "high", False

    # ── Faktablock (djupa, förberäknade nyckeltal — samma källa som analysen) ──
    fakta = [f"Bolag: {bolag}"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if omsattning_msek is not None:
        fakta.append(f"Omsättning: {omsattning_msek} MSEK")
    if varulager_msek is not None:
        fakta.append(f"Varulager: {varulager_msek} MSEK")
    if kpi.get("dos_dagar"):
        fakta.append(f"Days of Stock: ~{kpi['dos_dagar']} dagar "
                     f"({kpi.get('lageroms_hastighet','?')} lagervarv/år) mot branschnorm "
                     f"{kpi['dos_norm_lag']}–{kpi['dos_norm_hog']} dagar ({kpi['dos_norm_bransch']})")
    if kpi.get("overlager_msek"):
        fakta.append(f"Överlager mot norm: ~{kpi['overlager_dagar']} dagar ≈ "
                     f"{kpi['overlager_msek']} MSEK bundet över en sund nivå")
    if kpi.get("arlig_lagerkostnad_msek"):
        s = f"Årlig lagerhållningskostnad (~20% av varulagret): {kpi['arlig_lagerkostnad_msek']} MSEK"
        if kpi.get("lagerkostnad_andel_av_vinst_pct"):
            s += f" (~{kpi['lagerkostnad_andel_av_vinst_pct']}% av rörelseresultatet)"
        fakta.append(s)
    if kpi.get("frigorbart_lag_msek"):
        s = (f"DRÖMRESULTAT (ESTIMAT — presentera ALLTID som spann): ~{kpi['frigorbart_lag_msek']}–"
             f"{kpi['frigorbart_hog_msek']} MSEK frigörbart kapital "
             f"(15–25% av lagervärdet sitter erfarenhetsmässigt i döda/långsamma artiklar)")
        if kpi.get("frigorbart_manader_vinst"):
            s += f" — motsvarar ~{kpi['frigorbart_manader_vinst']} månaders vinst"
        fakta.append(s)
    if kpi.get("lagerandel_pct"):
        fakta.append(f"Lagerandel (varulager/oms): {kpi['lagerandel_pct']}%")

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
    if insights:
        krok_block = (
            "\nFÖRETAGSUNIKA KROKAR (förberäknade ur deras egna bokslut, RANGORDNADE — "
            "starkast först). ÖPPNA mejlet med 1–2 av dessa, ordagrant och verifierbart. "
            "Detta är det viktigaste i hela mejlet:\n"
            + "\n".join(f"  - {t}" for t in insights) + "\n")
        if headline:
            krok_block += (f"\nHEADLINE-VINKEL (får omformuleras, men behåll siffrorna): "
                           f"{headline}\n")
    else:
        krok_block = ("\n(Inga förberäknade krokar tillgängliga — öppna med senaste årets "
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
    )
    if followup_steg:
        user_msg += (
            f"\nDETTA ÄR EN UPPFÖLJNING (påminnelse nr {followup_steg}) — mottagaren "
            f"fick redan ett första mejl utan att svara. Håll det KORT (3–4 meningar), "
            f"öppna med en artig knuff ('hörde inte av dig — vill inte att det här ska "
            f"falla mellan stolarna'), ge EN ny konkret vinkel/värde (t.ex. en till "
            f"siffra ur trenden eller kostnaden av att vänta), och avsluta med samma "
            f"lätta 15-min-fråga. Upprepa inte hela det första mejlet.\n")
    user_msg += "Returnera JSON."

    if language == "en":
        user_msg += "\nOBS: Skriv mejlet på ENGELSKA (bolaget verkar internationellt)."

    # ── Anropa Claude ──────────────────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        # Modellen tänker innan den svarar och tänkandet räknas in i max_tokens —
        # 800 (gamla värdet) räckte inte till ett helt sexstyckesmejl.
        max_tokens=6000,
        system=_ROLE_INSTRUCTIONS[roll],
        messages=[{"role": "user", "content": user_msg}],
    )
    # Plocka bara textblocken (modellen kan inleda med ett tankeblock)
    raw = "".join(b.text for b in response.content if b.type == "text").strip()

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


_CALL_SYSTEM = """Du skriver ett kort TELEFONMANUS åt David Leifsson (Logistics Doctor) inför
ett uppföljningssamtal till ett bolag han redan mejlat om IHA (lager-/kapitalbindningsanalys).
Manuset ska vara pratbart, inte ett brev. Svenska, du-tilltal, avslappnat och rakt.

STRUKTUR (använd exakt dessa rubriker):
Öppning: en mening som presenterar David + varför han ringer (kopplat till mejlet).
Kroken: 1–2 företagsunika, verifierbara siffror om JUST detta bolag (helst flerårstrend).
Frågan: be om ett kort möte (15 min) — mjukt och konkret.
Om invändning: 2 korta repliker på 'har inte tid' / 'inte intresserad' / 'skicka info'.

REGLER: Max ~120 ord totalt. Hitta ALDRIG på siffror. Inga utropstecken. Punktlista där det
passar. Skriv så David kan läsa det rakt av i luren."""


def generate_call_script(bolag, namn="", titel="", bransch="", orgnr="", website="",
                         lagerandel=None, varulager_msek=None, omsattning_msek=None) -> str:
    """Kort, företagsunikt telefonmanus för ett uppföljningssamtal. '' vid fel."""
    # Samma berikning som mejlet: flerårstrend + vad bolaget gör.
    history = None
    if orgnr:
        try:
            from integrations import allabolag as _ab
            history = (_ab.get_financials(orgnr=orgnr) or {}).get("history")
        except Exception:
            history = None
    trends = _financial_trends(history)
    profil = _company_profile(website)
    dos = _dos(varulager_msek, omsattning_msek)
    fornamn = _first_name(namn) or "kontaktpersonen"

    fakta = [f"Bolag: {bolag}", f"Person att ringa: {namn or 'okänd'} ({titel or 'okänd roll'})"]
    if bransch:
        fakta.append(f"Bransch: {bransch}")
    if varulager_msek is not None:
        fakta.append(f"Varulager: {varulager_msek} MSEK")
    if lagerandel is not None:
        fakta.append(f"Lagerandel: {lagerandel}%")
    if dos is not None:
        fakta.append(f"Days of Stock: ~{dos} dagar")

    krok = ("\nFÖRETAGSUNIKA KROKAR (använd 1–2, ordagrant):\n"
            + "\n".join(f"  - {t}" for t in trends)) if trends else ""
    prof = (f"\nVad bolaget gör (hemsidan): {profil[:500]}") if profil else ""

    user_msg = (f"Skriv telefonmanus för uppföljningssamtal.\n\nFAKTA:\n"
                + "\n".join(f"  {f}" for f in fakta) + krok + prof
                + f"\n\nTilltala personen '{fornamn}'. Returnera bara manuset (ren text).")
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=3000, system=_CALL_SYSTEM,
            messages=[{"role": "user", "content": user_msg}])
        # Plocka bara textblocken (modellen kan inleda med ett tankeblock)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:
        return ""
