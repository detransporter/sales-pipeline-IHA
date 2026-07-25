"""
Röktest — klickar igenom varje sida i appens meny och kontrollerar att
ingen av dem kraschar.

Gör EXAKT det David tidigare gjorde manuellt efter varje ändring: laddar
startsidan, byter till varje flik i tur och ordning, och kollar att sidan
renderar rent (`at.exception` är tomt). Skillnaden är att det nu körs
automatiskt istället för att behöva klickas igenom för hand varje gång.

Ingen AI-koppling och ingen kostnad — sidorna gör bara vanliga (gratis)
Supabase-läsningar vid laddning, precis som en normal sidladdning i
webbläsaren. AI-anrop (mejlutkast, DM-generering m.m.) triggas bara av
knapptryck som testet aldrig simulerar.

Kräver riktig databaskoppling (SUPABASE_URL/SUPABASE_KEY i .env) — testet
är alltså inte en "ren" enhetstest utan en riktig sidladdning mot appens
egen databas, samma som `streamlit run app.py` skulle göra.

Kör med:
    pytest tests/test_app_smoke.py -v
eller helt enkelt:
    pytest
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

# Måste matcha PAGES-dictet i app.py (samma namn, samma ordning spelar
# ingen roll här). Lägger du till/döper om en sida i app.py — uppdatera
# listan här också, annars testar den inte den nya sidan.
PAGE_NAMES = [
    "🏠 Idag",
    "🔍 Hitta bolag",
    "🌱 Leads",
    "💬 Svar & uppföljning",
    "📅 Möten",
    "💰 Pipeline",
    "🧠 David Agent",
    "📊 Översikt",
    "📥 Kontakter",
]


def _started_app() -> AppTest:
    """Starta appen (motsvarar att öppna den i webbläsaren första gången)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception, f"Appen kraschade redan vid start: {at.exception}"
    return at


def test_startsidan_laddar_utan_krasch():
    _started_app()


def test_alla_sidor_renderar_utan_krasch():
    at = _started_app()
    for name in PAGE_NAMES:
        at.session_state["nav"] = name
        at.run()
        assert not at.exception, f"Sidan '{name}' kraschade: {at.exception}"
