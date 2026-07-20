"""
IHA Prospect Finder — Bolagsverket VärdefullaDatamängder API

API-flöde (ett org-nummer i taget):
  1. Token  POST /oauth2/token  (scope: vardefulla-datamangder:read ping)
  2. Lista  POST /dokumentlista  body: {"identitetsbeteckning": "XXXXXXXXXX"}
  3. Hämta  GET  /dokument/{dokumentId}  → ZIP med iXBRL-XHTML
  4. Parsa iXBRL: Nettoomsattning + Varulager
  5. Filtrera: lager_kvot > 30% AND nettoomsättning > 50 MSEK
  6. Exportera till prospects.csv + terminal-sammanfattning

Indata: en lista med org-nummer — antingen från --orgnr-fil (CSV/txt)
        eller inbyggd testlista (--test).
"""

import os
import sys
import io
import time
import zipfile
import argparse
import requests
import pandas as pd
from lxml import etree
from tqdm import tqdm
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

CLIENT_ID     = os.getenv("BV_CLIENT_ID")
CLIENT_SECRET = os.getenv("BV_CLIENT_SECRET")
TOKEN_URL     = os.getenv("BV_TOKEN_URL")
API_BASE      = os.getenv("BV_API_BASE")
SCOPE         = "vardefulla-datamangder:read vardefulla-datamangder:ping"

# iXBRL: möjliga namespace-URIer (varierar mellan dokument)
SE_GEN_BASE_URIS = [
    "http://www.taxonomier.se/se/fr/gen-base/2017-09-30",
    "http://www.taxonomier.se/se/fr/gaap/se-gen-base",
    "http://xbrl.taxonomier.se/se/fr/gaap/se-gen-base",
    "http://www.taxonomier.se/se/gaap/se-gen-base",
    "http://www.taxonomier.se/se/fr/misc-base/2017-09-30",
]

FILTER_LAGER_KVOT   = 0.30        # 30 %
FILTER_NETTOOMSATTR = 50_000_000  # 50 MSEK

# Godkända test-org-nummer (från "Testdata API Värdefulla datamängder.xlsx")
TEST_ORGNR = ["5567223705", "5560021361", "7164099017", "7020008350", "9124001992"]


# ── Token ─────────────────────────────────────────────────────────────

_token_cache: dict = {}

def get_token() -> str:
    import time as _time
    if _token_cache.get("token") and _token_cache.get("exp", 0) > _time.time() + 60:
        return _token_cache["token"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope":         SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"]   = _time.time() + data.get("expires_in", 3600)
    return _token_cache["token"]


def auth_headers(json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {get_token()}"}
    if json_body:
        h["Content-Type"] = "application/json"
        h["Accept"]       = "application/json"
    return h


# ── Steg 2: Dokumentlista per org ────────────────────────────────────

def fetch_document_list(orgnr: str) -> list[dict]:
    try:
        resp = requests.post(
            f"{API_BASE}/dokumentlista",
            headers=auth_headers(),
            json={"identitetsbeteckning": orgnr},
            timeout=10,
        )
    except requests.exceptions.Timeout:
        print(f"\n  Timeout för {orgnr} — hoppar över")
        return []
    if resp.status_code != 200:
        return []
    return resp.json().get("dokument", [])


# ── Steg 3 + 4: Hämta ZIP och parsa iXBRL ────────────────────────────

def _clean_num(text: str) -> float | None:
    t = (text or "").strip().replace("\xa0", "").replace(" ", "").replace(" ", "")
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_ixbrl(content: bytes) -> dict | None:
    try:
        root = etree.fromstring(content)
    except etree.XMLSyntaxError:
        try:
            root = etree.fromstring(content, etree.HTMLParser())
        except Exception:
            return None

    # iXBRL: finansiella värden ligger i ix:nonFraction med name-attribut
    # t.ex. <ix:nonFraction name="se-gen-base:Nettoomsattning">66 097 135</ix:nonFraction>
    values: dict[str, list[float]] = {}
    texts:  dict[str, str]         = {}

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        ln = etree.QName(el.tag).localname

        if ln in ("nonFraction", "nonNumeric"):
            name = el.get("name", "")
            local = name.split(":")[-1] if ":" in name else name
            text  = (el.text or "").strip()
            if not text:
                continue

            if ln == "nonFraction":
                v = _clean_num(text)
                if v is not None:
                    values.setdefault(local, []).append(v)
            else:
                if local not in texts:
                    texts[local] = text

    def best(candidates: list[str]) -> float | None:
        for c in candidates:
            vals = values.get(c, [])
            if vals:
                return max(vals, key=abs)
        return None

    def best_text(candidates: list[str]) -> str:
        for c in candidates:
            if c in texts:
                return texts[c]
        return ""

    return {
        "nettoomsattning": best(["Nettoomsattning"]),
        "varulager":       best(["VarulagerMm", "Varulager", "LagerFardigaVarorHandelsvaror"]),
        "foretagsnamn":    best_text(["ForetagetsNamn", "ForetagsNamn", "NamnPaForetaget"]),
        "rakenskapsaar":   best_text(["RakenskapsarSistaDag", "RakenskapsaretsSlut", "BalanceSheetDate"]),
    }


def fetch_and_parse_document(doc_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{API_BASE}/dokument/{doc_id}",
            headers={"Authorization": f"Bearer {get_token()}", "Accept": "*/*"},
            timeout=15,
        )
    except requests.exceptions.Timeout:
        return None
    if resp.status_code != 200:
        return None

    ct = resp.headers.get("Content-Type", "")
    if "zip" in ct or doc_id.endswith("_paket"):
        try:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile:
            return None
        # Parsa alla XHTML-filer i ZIP:en, returnera det med mest data
        best: dict | None = None
        for name in z.namelist():
            if name.lower().endswith((".xhtml", ".html", ".htm")):
                parsed = parse_ixbrl(z.read(name))
                if parsed and (parsed["nettoomsattning"] or parsed["varulager"]):
                    if best is None or (parsed["nettoomsattning"] or 0) > (best.get("nettoomsattning") or 0):
                        best = parsed
        return best
    else:
        return parse_ixbrl(resp.content)


# ── Steg 5: Filtrera ─────────────────────────────────────────────────

def filter_prospects(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df = df.dropna(subset=["nettoomsattning", "varulager"])
    df = df[df["nettoomsattning"] > 0]
    df["lager_kvot"] = df["varulager"] / df["nettoomsattning"]
    return df[(df["lager_kvot"] > FILTER_LAGER_KVOT) & (df["nettoomsattning"] >= FILTER_NETTOOMSATTR)].copy()


# ── Steg 6: Exportera + sammanfattning ───────────────────────────────

def export_csv(df: pd.DataFrame, path: str = "prospects.csv") -> None:
    out = pd.DataFrame({
        "orgnummer":            df["orgnummer"],
        "företagsnamn":         df["foretagsnamn"],
        "nettoomsättning_ksek": (df["nettoomsattning"] / 1000).round(0).astype(int),
        "varulager_ksek":       (df["varulager"] / 1000).round(0).astype(int),
        "lager_kvot_procent":   (df["lager_kvot"] * 100).round(1),
        "räkenskapsår":         df["rakenskapsaar"],
    }).sort_values("lager_kvot_procent", ascending=False)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\nExporterat till {path}")


def print_summary(total_orgnr: int, total_docs: int, prospects: pd.DataFrame) -> None:
    print("\n" + "═" * 60)
    print("IHA PROSPECT SCREENING — SAMMANFATTNING")
    print("═" * 60)
    print(f"Org-nummer sökta         : {total_orgnr}")
    print(f"Dokument parsade         : {total_docs}")
    print(f"Passerade filter         : {len(prospects)}")
    print(f"  (lager_kvot > {FILTER_LAGER_KVOT*100:.0f}% & omsättning > {FILTER_NETTOOMSATTR/1e6:.0f} MSEK)")

    if prospects.empty:
        print("\nInga prospects hittades.")
        return

    top = prospects.sort_values("lager_kvot", ascending=False).head(10)
    print(f"\nTOP {len(top)} PROSPECTS:")
    print("─" * 60)
    for _, row in top.iterrows():
        namn = (row.get("foretagsnamn") or row["orgnummer"])[:35]
        print(f"  {namn:<35}  {row['lager_kvot']*100:5.1f}%  {row['nettoomsattning']/1e6:7.1f} MSEK")
    print("─" * 60)


# ── Main ──────────────────────────────────────────────────────────────

def load_orgnr_list(path: str) -> list[str]:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        xl = pd.ExcelFile(path)
        # Välj rätt flik: "Allabolag lista" om den finns, annars första
        sheet = "Allabolag lista" if "Allabolag lista" in xl.sheet_names else xl.sheet_names[0]
        df = xl.parse(sheet, dtype=str, header=0)
    else:
        df = pd.read_csv(path, dtype=str, header=0)

    # Hitta kolumn med org-nummer (innehåller bindestreck + siffror, t.ex. 556074-3089)
    for col in df.columns:
        sample = df[col].dropna().head(5).tolist()
        cleaned = [str(v).strip().replace("-", "").replace(" ", "") for v in sample]
        if any(c.isdigit() and len(c) in (9, 10) for c in cleaned):
            result = []
            for v in df[col]:
                if pd.notna(v):
                    clean = str(v).strip().replace("-", "").replace(" ", "")
                    if clean.isdigit() and len(clean) in (9, 10):
                        result.append(clean)
            if result:
                return result

    raise ValueError(f"Kunde inte hitta en kolumn med org-nummer i {path}")


def load_orgnr_from_db() -> list[str]:
    # Återanvänd samma klient/schema-inställning som resten av appen istället
    # för en egen Supabase-koppling — annars märks inte ändringar i
    # database/supabase_client.py (schema, inloggning) här.
    from database.supabase_client import get_client
    sb = get_client()
    r = sb.table("lead_suggestions").select("orgnr").not_.is_("orgnr", "null").execute()
    result = []
    for row in r.data:
        v = str(row["orgnr"]).strip().replace("-", "").replace(" ", "")
        if v.isdigit() and len(v) in (9, 10):
            result.append(v)
    return list(dict.fromkeys(result))  # deduplicera, behåll ordning


def main():
    parser = argparse.ArgumentParser(description="IHA Prospect Finder via Bolagsverket API")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--test",      action="store_true", help="Använd inbyggda testorgnummer")
    src.add_argument("--orgnr-fil", metavar="FIL",       help="CSV/xlsx med org-nummer")
    src.add_argument("--fran-db",   action="store_true", help="Hämta org-nummer direkt från Supabase-databasen")
    parser.add_argument("--output", default="prospects.csv", help="Utdatafil (default: prospects.csv)")
    args = parser.parse_args()

    print("Hämtar OAuth2-token...")
    get_token()
    print("  Token OK.")

    if args.test:
        orgnr_lista = TEST_ORGNR
    elif args.fran_db:
        print("Hämtar org-nummer från databasen...")
        orgnr_lista = load_orgnr_from_db()
        print(f"  {len(orgnr_lista)} unika org-nummer i databasen.")
    else:
        orgnr_lista = load_orgnr_list(args.orgnr_fil)
    print(f"\nAntal org-nummer att söka: {len(orgnr_lista)}")

    records = []
    total_docs = 0

    for orgnr in tqdm(orgnr_lista, desc="Söker org-nummer", unit="org"):
        docs = fetch_document_list(orgnr)
        if not docs:
            continue

        for doc in docs:
            doc_id = doc.get("dokumentId")
            if not doc_id:
                continue
            total_docs += 1
            parsed = fetch_and_parse_document(doc_id)
            if parsed and (parsed["nettoomsattning"] or parsed["varulager"]):
                parsed["orgnummer"]    = orgnr
                parsed["rakenskapsaar"] = parsed.get("rakenskapsaar") or doc.get("rapporteringsperiodTom", "")
                records.append(parsed)
            time.sleep(0.1)

        time.sleep(0.2)

    print(f"\nParsade dokument med finansiell data: {len(records)} av {total_docs}")

    prospects = filter_prospects(records)

    if not prospects.empty:
        export_csv(prospects, args.output)

    print_summary(len(orgnr_lista), total_docs, prospects)


if __name__ == "__main__":
    main()
