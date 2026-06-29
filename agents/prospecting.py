import pandas as pd

# ── Scoring tables ─────────────────────────────────────────────────────────

TITLE_SCORES: dict[str, int] = {
    "cfo": 10,
    "chief financial officer": 10,
    "supply chain manager": 10,
    "supply chain director": 10,
    "head of supply chain": 10,
    "scm": 9,
    "inköpschef": 9,
    "purchasing manager": 9,
    "head of purchasing": 9,
    "procurement manager": 9,
    "operations manager": 8,
    "logistikchef": 8,
    "head of logistics": 8,
    "logistics manager": 8,
    "head of outbound logistics": 8,
    "head of inbound logistics": 8,
    "warehouse manager": 7,
    "vd": 7,
    "ceo": 7,
    "chief executive officer": 7,
    "ekonomichef": 7,
    "lagerchef": 6,
    "inköpare": 5,
    "buyer": 5,
    "planner": 5,
    "demand planner": 6,
    "supply planner": 6,
    "inventory manager": 8,
    "inventory controller": 7,
    "chief operations officer": 8,
    "coo": 8,
}

# LinkedIn uses English industry names
INDUSTRY_SCORES: dict[str, int] = {
    # Tillverkning / Manufacturing
    "manufacturing": 10,
    "motor vehicle manufacturing": 9,
    "automotive": 9,
    "industrial machinery manufacturing": 10,
    "food and beverage manufacturing": 8,
    "medical equipment manufacturing": 9,
    "pharmaceutical manufacturing": 8,
    "appliances, electrical, and electronics manufacturing": 10,
    "chemical manufacturing": 9,
    "plastics manufacturing": 9,
    "furniture and home furnishings manufacturing": 8,
    "packaging and containers manufacturing": 9,
    "printing services": 6,
    "aviation and aerospace component manufacturing": 10,
    # Distribution / Grossist
    "wholesale": 9,
    "wholesale building materials": 9,
    "distribution": 9,
    # Handel / E-handel
    "retail": 8,
    "consumer goods": 8,
    "e-commerce": 8,
    # Flyg / Transport
    "airlines and aviation": 10,
    "aviation": 10,
    "truck transportation": 7,
    "transportation, logistics, supply chain and storage": 7,
    "freight and package transportation": 7,
    "maritime transportation": 7,
}

MIN_SCORE = 5


def _score_title(title: str) -> int:
    title_lower = title.lower().strip()
    best = 0
    for key, pts in TITLE_SCORES.items():
        if key in title_lower:
            best = max(best, pts)
    return best


def _score_industry(industry: str) -> int:
    industry_lower = industry.lower().strip()
    # Exact match first
    if industry_lower in INDUSTRY_SCORES:
        return INDUSTRY_SCORES[industry_lower]
    # Partial match
    best = 0
    for key, pts in INDUSTRY_SCORES.items():
        if key in industry_lower or industry_lower in key:
            best = max(best, pts)
    return best


def score_prospect(titel: str, bransch: str) -> int:
    return _score_title(titel) + _score_industry(bransch)


def score_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add 'score' column and filter out rows below MIN_SCORE."""
    df = df.copy()
    df["score"] = df.apply(
        lambda row: score_prospect(
            str(row.get("titel", "")),
            str(row.get("bransch", "")),
        ),
        axis=1,
    )
    df = df[df["score"] >= MIN_SCORE].copy()
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    return df
