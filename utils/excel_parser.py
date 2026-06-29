import pandas as pd
from io import BytesIO

# Kolumnmappning — accepterar varianter
COLUMN_MAP = {
    "namn": ["namn", "name", "förnamn", "fullname", "full name"],
    "titel": ["titel", "title", "roll", "position", "job_title", "job title"],
    "bolag": ["bolag", "company", "företag", "organization", "organisation", "employer"],
    "bransch": ["bransch", "industry", "sektor", "sector"],
    "linkedin_url": ["linkedin_url", "linkedin", "url", "profil", "profile"],
    "telefon": ["telefon", "phone", "tel", "mobile", "mobil"],
    "extra_info": ["extra_info", "notes", "anteckningar", "info", "kommentar"],
}

REQUIRED = ["namn", "titel", "bolag", "bransch"]


def _normalize(col: str) -> str:
    return col.strip().lower().replace("-", "_").replace(" ", "_")


def _map_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Rename DataFrame columns to canonical names. Returns (renamed_df, missing_required)."""
    rename = {}
    normalized_cols = {_normalize(c): c for c in df.columns}

    for canonical, aliases in COLUMN_MAP.items():
        for alias in aliases:
            if _normalize(alias) in normalized_cols:
                rename[normalized_cols[_normalize(alias)]] = canonical
                break

    df = df.rename(columns=rename)

    # Drop unnamed columns
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]

    missing = [r for r in REQUIRED if r not in df.columns]
    return df, missing


def parse_excel(file) -> tuple[pd.DataFrame, list[str]]:
    """
    Parse uploaded Excel file.
    Returns (dataframe_with_canonical_columns, list_of_errors).
    file can be a file path (str) or a BytesIO / file-like object.
    """
    errors = []
    try:
        df = pd.read_excel(file, engine="openpyxl")
    except Exception as e:
        return pd.DataFrame(), [f"Kunde inte läsa filen: {e}"]

    if df.empty:
        return df, ["Filen är tom."]

    df, missing = _map_columns(df)

    if missing:
        errors.append(f"Saknade kolumner: {', '.join(missing)}")
        return df, errors

    # Drop rows with no name
    before = len(df)
    df = df[df["namn"].notna() & (df["namn"].astype(str).str.strip() != "")]
    dropped = before - len(df)
    if dropped:
        errors.append(f"Varning: {dropped} rader utan namn ignorerades.")

    # Ensure optional columns exist
    for col in ["linkedin_url", "telefon", "extra_info"]:
        if col not in df.columns:
            df[col] = ""

    # Fill NaN with empty string for text columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].fillna("")

    return df.reset_index(drop=True), errors


def dataframe_to_prospect_records(df: pd.DataFrame) -> list[dict]:
    """Convert parsed DataFrame rows to dicts ready for Supabase insert."""
    records = []
    for _, row in df.iterrows():
        records.append({
            "namn": str(row.get("namn", "")).strip(),
            "titel": str(row.get("titel", "")).strip(),
            "bolag": str(row.get("bolag", "")).strip(),
            "bransch": str(row.get("bransch", "")).strip(),
            "linkedin_url": str(row.get("linkedin_url", "")).strip(),
            "telefon": str(row.get("telefon", "")).strip(),
            "extra_info": str(row.get("extra_info", "")).strip(),
            "status": "ej_kontaktad",
        })
    return records
