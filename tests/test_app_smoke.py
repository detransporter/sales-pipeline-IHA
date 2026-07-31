"""
Röktest — klickar igenom varje sida i appens meny och kontrollerar att
ingen av dem kraschar.

Gör EXAKT det David tidigare gjorde manuellt efter varje ändring: laddar
startsidan, byter till varje flik i tur och ordning, och kollar att sidan
renderar rent (`at.exception` är tomt). Skillnaden är att det nu körs
automatiskt istället för att behöva klickas igenom för hand varje gång.

VIKTIGT — lösenordsspärren (2026-07-25, hittades av misstag): är
APP_PASSWORD satt (t.ex. i .streamlit/secrets.toml, som inte är samma sak
som .env) visar app.py en inloggningssida istället för själva appen.
Testet måste då kringgå den — annars renderar det bara login-skärmen om
och om igen, "lyckas" varje gång utan att någonsin ha testat en enda
riktig sida. (Det är precis vad som hände en gång: testet var grönt i
flera körningar utan att ha rört Leads-sidan alls.) Kringgåendet sätter
INTE något lösenord — det sätter samma `_authed`-flagga app.py:s egen
`_require_login()` sätter EFTER en lyckad inloggning, som en genväg runt
UI:t. `_verified_started_app()` nedan kollar dessutom att den renderade
titeln inte är inloggningssidans, så en framtida ändring som bryter
kringgåendet upptäcks direkt istället för att tyst ge samma falska "OK".

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


def _verified_started_app() -> AppTest:
    """
    Starta appen och kringgå en ev. lösenordsspärr (se modulens docstring).
    Kraschar testet direkt — med ett tydligt felmeddelande — om vi av någon
    anledning fortfarande fastnar på login-sidan efteråt, istället för att
    tyst fortsätta och ge ett meningslöst grönt test.
    """
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception, f"Appen kraschade redan vid start: {at.exception}"
    at.session_state["_authed"] = True
    at.run()
    assert not at.exception, f"Appen kraschade efter inloggningskringgåendet: {at.exception}"
    # Kolla på titelns INNEHÅLL, inte bara att någon titel finns —
    # inloggningssidan ritar också en st.title ("🔒 Sales pipeline - IHA"), så
    # ett blott `assert at.title` hade godkänts även där och gett exakt samma
    # falska grönt som en gång tidigare (se docstringen ovan).
    titles = [t.value for t in at.title]
    assert titles and not any("🔒" in t for t in titles), (
        f"Fastnade på inloggningssidan (titlar: {titles}). Testet skulle annars "
        "bara \"lyckas\" utan att ha testat en enda riktig sida. Kolla "
        "_require_login()/_authed i app.py."
    )
    return at


def test_startsidan_laddar_utan_krasch():
    _verified_started_app()


def test_alla_sidor_renderar_utan_krasch():
    at = _verified_started_app()
    for name in PAGE_NAMES:
        # Samma väg som appens egna "Gå till"-knappar: shared.goto() sätter
        # flaggan, app.py gör st.switch_page. Testar alltså den RIKTIGA
        # navigeringsvägen, inte bara att sidfunktionen går att anropa.
        at.session_state["_goto"] = name
        at.run()
        assert not at.exception, f"Sidan '{name}' kraschade: {at.exception}"
