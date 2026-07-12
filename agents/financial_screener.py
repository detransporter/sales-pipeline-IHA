"""
Financial screener — hittar bolag där kapital bevisligen sitter fast i lager.

Tar ekonomi från Allabolag och applicerar Davids IHA-kriterier. Detta är den nya
tratt-toppen: istället för att gissa vilka bolag som har lagerproblem ser vi det
direkt i siffrorna.

KPI:er (standardvärden, justerbara i UI):
  - Omsättning 50–300 MSEK            (rätt storlek — SME, inte koncern)
  - Anställda ≤ 200                    ("inte för många")
  - Lagerandel = Varulager / Omsättning > 20%   ← kärnsignalen, mycket bundet kapital
  - Vinstmarginal = Resultat / Omsättning < 3%  (pressad lönsamhet → starkt case)

IHA-score rankar kvalificerade bolag: hög lagerandel + låg/negativ marginal = högst prio.
"""

# Nyckelord per bransch för keyword-sweep (komplement till segmentering).
# Segmentering hittar bolag via finansiell storlek; keyword-sweep hittar bolag
# via branschbeskrivning. Tillsammans täcker de varandras blindfläckar.
# "Alla lager-tunga" = kurerade topptermer, inte hela listan (annars för långsamt).
BRANSCH_KEYWORDS: dict[str, list[str]] = {
    "Alla lager-tunga branscher": [
        "grossist", "partihandel", "tillverkning", "industri",
        "metallindustri", "plasttillverkning", "livsmedelstillverkning",
        "byggvaruhandel", "elektroniktillverkning", "distribution",
    ],
    "Tillverkning (generellt)":           ["tillverkning", "tillverkande företag", "industri"],
    "Plasttillverkning":                  ["plasttillverkning", "plastindustri", "formsprutning"],
    "Metall & verkstad":                  ["metallindustri", "verkstadsindustri", "legotillverkning metall"],
    "Industriell utrustning/maskiner":    ["maskintillverkning", "industriutrustning", "maskinindustri"],
    "Livsmedelstillverkning":             ["livsmedelstillverkning", "livsmedelsindustri", "livsmedelsproducent"],
    "Möbel & inredning":                  ["möbeltillverkning", "möbelindustri", "inredningstillverkning"],
    "Kemi & plast":                       ["kemisk industri", "kemikalietillverkning", "plastindustri"],
    "Medtech":                            ["medicinteknik", "medicintekniska produkter", "medtech tillverkning"],
    "Förpackning":                        ["förpackningstillverkning", "förpackningsindustri", "emballage"],
    "Elektronik/kontraktstillverkning":   ["elektroniktillverkning", "kontraktstillverkning", "legotillverkning elektronik"],
    "Grossist (generellt)":               ["grossist", "partihandel", "grossisthandel"],
    "Bygggrossist":                       ["byggvaruhandel", "byggmaterial grossist", "bygggrossist"],
    "Distribution/partihandel":           ["distribution", "partihandel", "import grossist"],
    "E-handel med lager":                 ["e-handel", "näthandel lager", "postorder"],
}

# Kurerade ICP-branscher → flera sökord (poolas för bredare träff). Rullgardin i UI.
BRANSCHER: dict[str, list[str]] = {
    "Tillverkning (generellt)": ["tillverkning", "tillverkande företag", "industri"],
    "Plasttillverkning": ["plasttillverkning", "plastindustri", "formsprutning"],
    "Metall & verkstad": ["metallindustri", "verkstadsindustri", "legotillverkning metall"],
    "Industriell utrustning/maskiner": ["maskintillverkning", "industriutrustning", "maskinindustri"],
    "Livsmedelstillverkning": ["livsmedelstillverkning", "livsmedelsindustri", "livsmedelsproducent"],
    "Möbel & inredning": ["möbeltillverkning", "möbelindustri", "inredningstillverkning"],
    "Kemi & plast": ["kemisk industri", "kemikalietillverkning", "plastindustri"],
    "Medtech": ["medicinteknik", "medicintekniska produkter", "medtech tillverkning"],
    "Förpackning": ["förpackningstillverkning", "förpackningsindustri", "emballage"],
    "Elektronik/kontraktstillverkning": ["elektroniktillverkning", "kontraktstillverkning", "legotillverkning elektronik"],
    "Grossist (generellt)": ["grossist", "partihandel", "grossisthandel"],
    "Bygggrossist": ["byggvaruhandel", "byggmaterial grossist", "bygggrossist"],
    "Distribution/partihandel": ["distribution", "partihandel", "import grossist"],
    "E-handel med lager": ["e-handel", "näthandel lager", "postorder"],
}

# Bransch → SNI-kodprefix (SNI 2007). Post-filtrerar Segmenterings-träffar så vi
# bara behåller lager-tunga industrier. Ett bolags nace_code (t.ex. "16120") matchar
# ett prefix om koden börjar på det.
BRANSCH_SNI: dict[str, list[str]] = {
    "Alla lager-tunga branscher": [str(i) for i in range(10, 34)] + ["46", "479"],
    "Tillverkning (generellt)": [str(i) for i in range(10, 34)],
    "Plasttillverkning": ["22"],
    "Metall & verkstad": ["24", "25", "28", "33"],
    "Industriell utrustning/maskiner": ["27", "28"],
    "Livsmedelstillverkning": ["10", "11"],
    "Möbel & inredning": ["31"],
    "Kemi & plast": ["20", "21", "22"],
    "Medtech": ["325", "266"],
    "Förpackning": ["17", "222"],
    "Elektronik/kontraktstillverkning": ["26", "27"],
    "Grossist (generellt)": ["46"],
    "Bygggrossist": ["466", "4673", "4674"],
    "Distribution/partihandel": ["46"],
    "E-handel / postorder": ["479"],
}


def nace_matches(nace_code: str, prefixes: list[str]) -> bool:
    """True om bolagets SNI-kod börjar på något av prefixen."""
    code = (nace_code or "").strip()
    return bool(code) and any(code.startswith(p) for p in prefixes)


# Sveriges 21 län — används för rikstäckande svep ("Hela Sverige").
LAN: list[str] = [
    "Stockholm", "Uppsala", "Södermanland", "Östergötland", "Jönköping",
    "Kronoberg", "Kalmar", "Gotland", "Blekinge", "Skåne", "Halland",
    "Västra Götaland", "Värmland", "Örebro", "Västmanland", "Dalarna",
    "Gävleborg", "Västernorrland", "Jämtland", "Västerbotten", "Norrbotten",
]

# Rullgardin för ort: rikssvep, enskilda län, plus några landskap.
ORTER: list[str] = ["Hela Sverige (svep alla län)"] + LAN + ["Småland", "Mälardalen", "Norrland"]

# Standardtrösklar (UI kan skicka egna)
DEFAULT_OMS_MIN = 50.0     # MSEK
DEFAULT_OMS_MAX = 300.0    # MSEK
DEFAULT_MAX_ANSTALLDA = 200
DEFAULT_MIN_LAGERANDEL = 20.0   # %
DEFAULT_MAX_MARGINAL = 3.0      # %


def compute_kpis(fin: dict) -> dict:
    """Räkna ut lagerandel, vinstmarginal och bruttomarginal (%) ur en Allabolag-ekonomi-dict."""
    oms = fin.get("omsattning_msek")
    varulager = fin.get("varulager_msek") or 0.0
    resultat = fin.get("resultat_msek")

    lagerandel = round(varulager / oms * 100, 1) if oms else None
    vinstmarginal = round(resultat / oms * 100, 1) if (oms and resultat is not None) else None
    # Bruttomarginal kommer direkt från Allabolag (TR-kod) — redan beräknad i allabolag.py
    bruttomarginal = fin.get("bruttomarginal")
    return {
        "lagerandel": lagerandel,
        "vinstmarginal": vinstmarginal,
        "bruttomarginal": bruttomarginal,
    }


def iha_score(lagerandel: float | None, vinstmarginal: float | None,
              bruttomarginal: float | None = None) -> int:
    """
    Ju mer kapital i lager och ju sämre lönsamhet, desto högre prio.

    Dimensioner:
      lagerandel     — primär drivare (20–100+ → direkt påslag)
      vinstmarginal  — nettolönsamhet: pressad marginal förstärker caset
      bruttomarginal — täckningsgrad: låg bruttomarginal = prispress =
                       bolaget har råd minst av allt att ha dött lager
    """
    score = 0.0
    if lagerandel is not None:
        score += lagerandel                        # 20–100+ → driver scoren
    if vinstmarginal is not None:
        score += max(0.0, 10.0 - vinstmarginal)    # 3% → +7, förlust → +10–20
    if bruttomarginal is not None:
        # Låg täckning = stark prispress = starkare IHA-case
        if bruttomarginal < 10:
            score += 12
        elif bruttomarginal < 20:
            score += 7
        elif bruttomarginal < 35:
            score += 3
        # >35% → inga poäng (hälsosam marginal)
    return int(round(score))


def passes_prefilter(c: dict,
                     oms_min: float = DEFAULT_OMS_MIN,
                     oms_max: float = DEFAULT_OMS_MAX,
                     max_anstallda: int = DEFAULT_MAX_ANSTALLDA,
                     max_marginal: float = DEFAULT_MAX_MARGINAL,
                     require_revenue: bool = False,
                     hard_margin: bool = False) -> bool:
    """
    Snabbt förfilter på söklistans data (omsättning, resultat, anställda) UTAN varulager.
    Avgör om ett bolag är värt en detaljhämtning för varulager.

    require_revenue=False: behåll bolag som saknar omsättningsdata (försiktigt, en sökning).
    require_revenue=True:  kräv omsättning i band (för stora svep, så vi bara djupgranskar
                           bolag i rätt storlek).
    hard_margin=False (standard): marginal är ett MJUKT kriterium — den utesluter inte bolag
                           här, utan rankas via iha_score senare (lönsamma bolag kan binda
                           mycket lagerkapital och är fortfarande bra IHA-kunder).
    hard_margin=True:      kräv marginal under taket redan i förfiltret (gammalt beteende —
                           bara pressade bolag djupgranskas).
    """
    oms = c.get("omsattning_msek")
    if oms is None:
        return not require_revenue
    if oms < oms_min or oms > oms_max:
        return False
    anst = c.get("anstallda")
    if anst is not None and anst > max_anstallda:
        return False
    if hard_margin:
        res = c.get("resultat_msek")
        if res is not None and oms:
            if res / oms * 100 >= max_marginal:
                return False
        elif require_revenue and res is None:
            return False
    return True


def screen_company(fin: dict,
                   oms_min: float = DEFAULT_OMS_MIN,
                   oms_max: float = DEFAULT_OMS_MAX,
                   max_anstallda: int = DEFAULT_MAX_ANSTALLDA,
                   min_lagerandel: float = DEFAULT_MIN_LAGERANDEL,
                   max_marginal: float = DEFAULT_MAX_MARGINAL,
                   hard_margin: bool = False) -> dict:
    """
    Bedöm ETT bolag mot kriterierna. Returnerar fin + kpis + 'passar' + 'skäl' + 'iha_score'.
    'skäl' listar varför ett bolag föll bort (tom lista om det passerar).

    Kärnkravet är lagerandel ≥ min_lagerandel (bundet kapital). Marginal är som standard
    MJUK: den utesluter inte, utan höjer iha_score när den är låg/negativ. Sätt
    hard_margin=True för att åter göra marginal < max_marginal till ett hårt krav.
    """
    kpis = compute_kpis(fin)
    lagerandel = kpis["lagerandel"]
    marginal = kpis["vinstmarginal"]
    bruttomarginal = kpis["bruttomarginal"]
    oms, anst = fin.get("omsattning_msek"), fin.get("anstallda")

    skal: list[str] = []
    if oms is None:
        skal.append("saknar omsättningsdata")
    else:
        if oms < oms_min:
            skal.append(f"omsättning {oms} < {oms_min} MSEK")
        if oms > oms_max:
            skal.append(f"omsättning {oms} > {oms_max} MSEK")
    if anst is not None and anst > max_anstallda:
        skal.append(f"{anst} anställda > {max_anstallda}")
    if lagerandel is None:
        skal.append("kan ej räkna lagerandel")
    elif lagerandel < min_lagerandel:
        skal.append(f"lagerandel {lagerandel}% < {min_lagerandel}%")
    if hard_margin and marginal is not None and marginal >= max_marginal:
        skal.append(f"marginal {marginal}% ≥ {max_marginal}%")

    result = dict(fin)
    result.update(kpis)
    result["passar"] = len(skal) == 0
    result["skäl"] = skal
    result["iha_score"] = iha_score(lagerandel, marginal, bruttomarginal)
    return result


def screen_companies(fins: list[dict], **filters) -> dict:
    """
    Screena en lista bolags-ekonomier. Returnerar:
      {"qualified": [...sorterade på iha_score], "rejected": [...], "no_data": [...]}
    """
    qualified, rejected, no_data = [], [], []
    for fin in fins:
        if not fin or fin.get("omsattning_msek") is None:
            no_data.append(fin or {})
            continue
        res = screen_company(fin, **filters)
        (qualified if res["passar"] else rejected).append(res)
    qualified.sort(key=lambda r: r["iha_score"], reverse=True)
    return {"qualified": qualified, "rejected": rejected, "no_data": no_data}
