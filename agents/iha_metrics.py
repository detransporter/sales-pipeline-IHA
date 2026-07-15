"""
IHA-metrik — deterministisk KPI-motor för föranalys och mejl.

Räknar ut ALLT vi ärligt kan härleda ur publika bokslut (Allabolag): riktig
lageromsättningshastighet och Days of Stock via bruttomarginal, flerårstrender
(CAGR lager vs omsättning), lagerhållningskostnad som andel av vinsten,
frigörbart kapital i kronor och månader av vinst, samt en jämförelse mot
branschnorm. INGEN AI, INGA gissade tal — bara aritmetik på verifierbara siffror.
Saknas data returneras None för just den nyckeln (aldrig en påhittad siffra).

Både `agents/company_analyzer.py` och `agents/email_writer.py` använder detta så
att analysen och mejlet alltid vilar på exakt samma siffror.
"""

from __future__ import annotations

CARRYING_COST_PCT = 0.20      # årlig lagerhållningskostnad ~20 % av varulagervärdet
RELEASE_LOW_PCT = 0.15        # försiktig uppskattning av frigörbart kapital
RELEASE_HIGH_PCT = 0.25       # optimistisk uppskattning

# Branschnormer för Days of Stock (dagar). Grova men vedertagna spann; används
# bara för att positionera, alltid som spann. Nyckelord matchas mot bransch-texten.
_DOS_NORMS = [
    (("tillverk", "manufactur", "industri", "produktion", "verkstad", "fabrik"),
     (60, 90), "tillverkning"),
    (("grossist", "wholesale", "distribu", "import", "parti", "förnödenhet",
      "reservdel", "komponent", "tillbehör", "leverantör av", "handel med",
      "agentur", "grossisthandel"), (30, 45), "grossist/distribution"),
    (("detalj", "retail", "butik", "e-handel", "ehandel", "webshop", "handel"),
     (30, 60), "handel"),
    (("bygg", "construction", "installation", "vvs", "el-"), (45, 75),
     "bygg/installation"),
]
_DOS_NORM_DEFAULT = (45, 75)

# Affärsmodell → DOS-norm. Styr benchmark när modellen är känd (klassad från
# hemsidan) i stället för att gissa på den tvetydiga bransch-etiketten.
_MODEL_NORMS = {
    "tillverkning": ((60, 90), "tillverkning"),
    "grossist": ((30, 45), "grossist/distribution"),
    "handel": ((30, 60), "handel"),
    "bygg": ((45, 75), "bygg/installation"),
}
BUSINESS_MODELS = ("tillverkning", "grossist", "handel", "bygg")


def norm_for(affarsmodell: str = "", bransch: str = "") -> tuple[tuple[int, int], str]:
    """Rätt DOS-norm: affärsmodell först (om känd), annars gissa på branschtext."""
    m = (affarsmodell or "").strip().lower()
    if m in _MODEL_NORMS:
        return _MODEL_NORMS[m]
    return _dos_norm(bransch)


def _f(x):
    """Tolerant float-konvertering → float eller None."""
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _dos_norm(bransch: str) -> tuple[tuple[int, int], str]:
    b = (bransch or "").lower()
    for keys, span, label in _DOS_NORMS:
        if any(k in b for k in keys):
            return span, label
    return _DOS_NORM_DEFAULT, "generell SME"


def _cagr(old, new, years: int):
    """Årlig tillväxttakt i procent. None om ej beräkningsbar."""
    o, n = _f(old), _f(new)
    if o is None or n is None or o <= 0 or n <= 0 or years <= 0:
        return None
    return round(((n / o) ** (1 / years) - 1) * 100, 1)


def _total_growth_pct(old, new):
    o, n = _f(old), _f(new)
    if o is None or n is None or o <= 0:
        return None
    return round((n - o) / o * 100)


def compute(
    bolag: str = "",
    bransch: str = "",
    omsattning_msek=None,
    varulager_msek=None,
    resultat_msek=None,
    bruttomarginal=None,
    anstallda=None,
    lagerandel=None,
    history=None,
    affarsmodell: str = "",
) -> dict:
    """
    Returnerar en dict med:
      kpi        – deterministiska nyckeltal (None där data saknas)
      insights   – rangordnade, färdigformulerade krokar (starkast först)
      headline   – EN slagkraftig mening (eller "")
      caveats    – förbehåll som håller analysen trovärdig
    """
    oms = _f(omsattning_msek)
    vl = _f(varulager_msek)
    res = _f(resultat_msek)
    bm = _f(bruttomarginal)
    kpi: dict = {}

    # ── COGS → riktig omsättningshastighet & Days of Stock ──────────────────
    cogs = None
    if oms and bm is not None and 0 < bm < 100:
        cogs = round(oms * (1 - bm / 100), 1)
        kpi["cogs_msek"] = cogs
        kpi["bruttomarginal_pct"] = round(bm, 1)

    if cogs and vl and vl > 0:
        turns = cogs / vl
        kpi["lageroms_hastighet"] = round(turns, 1)          # ggr/år
        kpi["dos_dagar"] = round(365 / turns) if turns > 0 else None
        kpi["dos_metod"] = "COGS-baserad (via bruttomarginal)"
    elif oms and vl and vl > 0:
        # Fallback: försäljningsbaserad DOS (grövre, saknar bruttomarginal).
        kpi["dos_dagar"] = round(vl * 365 / oms)
        kpi["dos_metod"] = "försäljningsbaserad (bruttomarginal saknas)"
        if oms > 0:
            kpi["lageroms_hastighet"] = round(oms / vl, 1)

    # ── Lagerandel ──────────────────────────────────────────────────────────
    if lagerandel is not None:
        kpi["lagerandel_pct"] = round(_f(lagerandel), 1) if _f(lagerandel) else None
    elif oms and vl and oms > 0:
        kpi["lagerandel_pct"] = round(vl / oms * 100, 1)

    # ── Lagerhållningskostnad & frigörbart kapital ──────────────────────────
    if vl and vl > 0:
        kpi["varulager_msek"] = round(vl, 1)
        carry = round(vl * CARRYING_COST_PCT, 1)
        kpi["arlig_lagerkostnad_msek"] = carry
        kpi["kostnad_per_dag_kr"] = round(carry * 1_000_000 / 365)
        kpi["frigorbart_lag_msek"] = round(vl * RELEASE_LOW_PCT, 1)
        kpi["frigorbart_hog_msek"] = round(vl * RELEASE_HIGH_PCT, 1)
        if res and res > 0:
            kpi["lagerkostnad_andel_av_vinst_pct"] = round(carry / res * 100)
            # Frigörbart kapital uttryckt i månaders vinst (försiktiga spannet).
            kpi["frigorbart_manader_vinst"] = round(kpi["frigorbart_lag_msek"] / (res / 12), 1)

    # ── Branschbenchmark: överlager i dagar → kronor ────────────────────────
    (norm_lo, norm_hi), norm_label = norm_for(affarsmodell, bransch)
    kpi["dos_norm_lag"], kpi["dos_norm_hog"], kpi["dos_norm_bransch"] = norm_lo, norm_hi, norm_label
    kpi["affarsmodell"] = (affarsmodell or "").strip().lower() if \
        (affarsmodell or "").strip().lower() in _MODEL_NORMS else ""
    kpi["affarsmodell_kalla"] = "klassad" if kpi["affarsmodell"] else "gissad (branschord)"
    dos = kpi.get("dos_dagar")
    if dos and dos > norm_hi:
        excess_days = dos - norm_hi
        kpi["overlager_dagar"] = excess_days
        # Överlager i kronor = extra dagar × daglig lagerförbrukning.
        daglig = (cogs if cogs else oms)
        if daglig and daglig > 0:
            kpi["overlager_msek"] = round(excess_days * daglig / 365, 1)

    # ── Flerårstrend (CAGR lager vs omsättning) ─────────────────────────────
    rows = [h for h in (history or [])
            if _f(h.get("omsattning_msek")) or _f(h.get("varulager_msek"))]
    trend: dict = {}
    if len(rows) >= 2:
        new, old = rows[0], rows[-1]                 # nyast först i listan
        # Kalenderspann om åren är läsbara, annars antal intervall.
        span_years = max(1, len(rows) - 1)
        try:
            diff = int(str(new.get("år"))[:4]) - int(str(old.get("år"))[:4])
            if diff > 0:
                span_years = diff
        except (TypeError, ValueError):
            pass
        trend = {
            "ar_gammal": old.get("år", ""), "ar_ny": new.get("år", ""),
            "ar_span": span_years,
            "varulager_cagr_pct": _cagr(old.get("varulager_msek"),
                                        new.get("varulager_msek"), span_years),
            "omsattning_cagr_pct": _cagr(old.get("omsattning_msek"),
                                         new.get("omsattning_msek"), span_years),
            "varulager_tillvaxt_pct": _total_growth_pct(old.get("varulager_msek"),
                                                        new.get("varulager_msek")),
            "omsattning_tillvaxt_pct": _total_growth_pct(old.get("omsattning_msek"),
                                                         new.get("omsattning_msek")),
            "varulager_gammal": _f(old.get("varulager_msek")),
            "varulager_ny": _f(new.get("varulager_msek")),
            "omsattning_gammal": _f(old.get("omsattning_msek")),
            "omsattning_ny": _f(new.get("omsattning_msek")),
        }
        kpi["trend"] = trend

    insights = _build_insights(kpi, trend, bolag)
    headline = _build_headline(kpi, trend, bolag)
    caveats = _build_caveats(kpi, rows)

    return {"kpi": kpi, "insights": insights, "headline": headline, "caveats": caveats}


def _build_insights(kpi: dict, trend: dict, bolag: str) -> list[str]:
    """Färdiga krokar, rangordnade så den starkaste ligger först."""
    out: list[str] = []

    # 1. Divergens: lager upp MEDAN omsättning ner — starkast.
    vg, vn = trend.get("varulager_gammal"), trend.get("varulager_ny")
    og, on = trend.get("omsattning_gammal"), trend.get("omsattning_ny")
    if vg and vn and og and on and vn > vg and on < og:
        out.append(
            f"Lagret växer medan försäljningen faller: varulager {vg}→{vn} MSEK "
            f"men omsättning {og}→{on} MSEK ({trend['ar_gammal']}→{trend['ar_ny']}) "
            f"— klassiskt tecken på kapital som fastnar i hyllan.")

    # 2. Lager växer snabbare än försäljningen (CAGR-gap).
    vc, oc = trend.get("varulager_cagr_pct"), trend.get("omsattning_cagr_pct")
    if vc is not None and oc is not None and vc - oc >= 5:
        out.append(
            f"Lagret växer {vc}%/år men omsättningen bara {oc}%/år "
            f"({trend['ar_gammal']}–{trend['ar_ny']}) — lagret drar ifrån försäljningen.")
    elif trend.get("varulager_tillvaxt_pct") and trend["varulager_tillvaxt_pct"] >= 25:
        out.append(
            f"Varulagret växte {trend['varulager_tillvaxt_pct']}% "
            f"({trend.get('varulager_gammal')}→{trend.get('varulager_ny')} MSEK) "
            f"mellan {trend['ar_gammal']} och {trend['ar_ny']}.")

    # 3. Days of Stock mot branschnorm → överlager i kronor.
    if kpi.get("overlager_msek"):
        out.append(
            f"Days of Stock ~{kpi['dos_dagar']} dagar mot branschnorm "
            f"{kpi['dos_norm_lag']}–{kpi['dos_norm_hog']} dagar "
            f"({kpi['dos_norm_bransch']}) — ~{kpi['overlager_dagar']} dagars överlager, "
            f"motsvarar ~{kpi['overlager_msek']} MSEK bundet över en sund nivå.")
    elif kpi.get("dos_dagar"):
        out.append(f"Days of Stock ~{kpi['dos_dagar']} dagar "
                   f"({kpi.get('lageroms_hastighet')} lagervarv/år).")

    # 4. Lagerkostnaden äter vinsten.
    if kpi.get("lagerkostnad_andel_av_vinst_pct"):
        out.append(
            f"Den årliga lagerhållningskostnaden (~{kpi['arlig_lagerkostnad_msek']} MSEK) "
            f"motsvarar ~{kpi['lagerkostnad_andel_av_vinst_pct']}% av rörelseresultatet.")

    # 5. Frigörbart kapital i kronor (+ månaders vinst).
    if kpi.get("frigorbart_lag_msek"):
        s = (f"Uppskattat frigörbart kapital: {kpi['frigorbart_lag_msek']}–"
             f"{kpi['frigorbart_hog_msek']} MSEK (15–25% av lagervärdet)")
        if kpi.get("frigorbart_manader_vinst"):
            s += f" — motsvarar ~{kpi['frigorbart_manader_vinst']} månaders vinst"
        out.append(s + ".")

    return out


def _build_headline(kpi: dict, trend: dict, bolag: str) -> str:
    """EN slagkraftig mening att öppna med."""
    lo, hi = kpi.get("frigorbart_lag_msek"), kpi.get("frigorbart_hog_msek")
    per_dag = kpi.get("kostnad_per_dag_kr")
    if lo and hi:
        s = f"Vi uppskattar att {lo}–{hi} MSEK ligger onödigt bundet i lager"
        if per_dag:
            s += f" — det kostar er ~{per_dag:,} kr per dag att lagerhålla".replace(",", " ")
        if kpi.get("frigorbart_manader_vinst"):
            s += f", motsvarande ~{kpi['frigorbart_manader_vinst']} månaders vinst"
        return s + "."
    if kpi.get("overlager_msek"):
        return (f"~{kpi['overlager_msek']} MSEK ligger bundet över en sund lagernivå "
                f"för branschen.")
    return ""


def _build_caveats(kpi: dict, rows: list) -> list[str]:
    out = []
    if kpi.get("dos_metod", "").startswith("försäljnings"):
        out.append("Days of Stock är försäljningsbaserad (bruttomarginal saknades) "
                   "— tolka som grov indikation.")
    else:
        out.append("Days of Stock bygger på bruttomarginal ur bokslutet — presenteras "
                   "som spann, inte exakt siffra.")
    out.append("Varulagret är en bokslutsögonblicksbild (kan vara säsong).")
    if len(rows) < 3:
        out.append("Begränsad historik — trenden vilar på få år.")
    out.append("Detta är en hypotes ur publika bokslut. IHA:t bekräftar den på "
               "artikelnivå (ABC, dödlager, ledtider).")
    return out
