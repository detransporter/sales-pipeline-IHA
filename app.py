"""
Sales pipeline - IHA — huvudfil.

Den här filen gör bara två saker:
  1. Ritar menyn i sidopanelen (tratt-ordning).
  2. Skickar vidare till rätt sida i `views/`.

All logik per sida bor i `views/<sida>.py`. Gemensamma hjälpare och konstanter
bor i `views/shared.py`. Vill du ändra en sida — öppna bara den filen.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

# ── Secrets-brygga ───────────────────────────────────────────────────────────
# Lokalt kommer nycklarna från .env (via python-dotenv). På Streamlit Cloud finns
# ingen .env — där ligger de i st.secrets. Vi speglar st.secrets → os.environ så
# att all os.getenv()-kod funkar oförändrat i båda miljöerna. MÅSTE ligga före
# modulimporterna nedan, eftersom vissa läser nycklar redan vid import.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str) and _k not in os.environ:
            os.environ[_k] = _v
except Exception:
    pass

from views import (today, find_companies, leads, replies, meetings, pipeline,
                   agent, overview, import_contacts, shared)

st.set_page_config(
    page_title="Sales pipeline - IHA",
    page_icon="📊",
    layout="wide",
)


# ── Inloggning ───────────────────────────────────────────────────────────────
# Aktiveras bara om APP_PASSWORD är satt (dvs online). Lokalt utan lösenord är
# appen öppen. På Streamlit Cloud skyddar detta appen även om länken är publik.
#
# Inloggningen sparas som en biljett i URL:en (?auth=...) — session_state lever
# bara i serverminnet och nollas vid varje deploy/omstart/avbruten anslutning,
# vilket förr loggade ut David flera gånger om dagen. Biljetten är en HMAC av
# lösenordet (inte lösenordet självt) och ligger kvar i flikens URL, så
# inloggningen överlever allt utom att lösenordet byts. OBS: dela aldrig
# URL:en med ?auth= i — den släpper in innehavaren utan lösenord.
import hashlib
import hmac


def _auth_token(pw: str) -> str:
    return hmac.new(pw.encode(), b"iha-app-login-v1", hashlib.sha256).hexdigest()[:32]


def _require_login() -> bool:
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return True
    token = _auth_token(pw)

    # Giltig biljett i URL:en → inloggad (överlever omstarter och deploys).
    # Flaggan MÅSTE sättas här, inte bara vid lösenordsinmatning: st.navigation
    # raderar hela query-strängen vid varje menyklick, så utan flaggan hade
    # första klicket i menyn kastat ut dig till inloggningsrutan igen (biljetten
    # borta ur URL:en OCH ingen session att falla tillbaka på).
    if hmac.compare_digest(st.query_params.get("auth", ""), token):
        st.session_state["_authed"] = True
        return True

    # Inloggad i denna session men biljetten saknas i URL:en (t.ex. just efter
    # ett menyklick) → lägg tillbaka den, så inloggningen överlever även en
    # omstart/deploy när sessionsminnet nollställs.
    if st.session_state.get("_authed"):
        st.query_params["auth"] = token
        return True

    st.title("🔒 Sales pipeline - IHA")
    st.caption("Logga in för att fortsätta.")
    entered = st.text_input("Lösenord", type="password")
    if entered:
        if entered == pw:
            st.session_state["_authed"] = True
            st.query_params["auth"] = token
            st.rerun()
        st.error("Fel lösenord.")
    return False


if not _require_login():
    st.stop()


# ── Sidor ────────────────────────────────────────────────────────────────────
# Ordningen här är ordningen i menyn (tratten uppifrån och ner + verktyg sist).
#
# Nycklarna är oförändrade sedan menyn var en radioknapp — shared.goto() och
# alla "Gå till"-knappar i views/ använder dem, så de får inte döpas om utan
# att anropsställena ändras samtidigt.
#
# url_path MÅSTE anges explicit: alla sidfunktioner heter `render`, och utan
# url_path härleder Streamlit adressen ur funktionsnamnet — då skulle alla nio
# sidor krocka på samma adress.
PAGES = {
    "🏠 Idag": st.Page(today.render, title="Idag", icon="🏠",
                       url_path="idag", default=True),
    "🔍 Hitta bolag": st.Page(find_companies.render, title="Hitta bolag",
                              icon="🔍", url_path="hitta-bolag"),
    "🌱 Leads": st.Page(leads.render, title="Leads", icon="🌱",
                        url_path="leads"),
    "💬 Svar & uppföljning": st.Page(replies.render, title="Svar & uppföljning",
                                     icon="💬", url_path="svar"),
    "📅 Möten": st.Page(meetings.render, title="Möten", icon="📅",
                        url_path="moten"),
    "💰 Pipeline": st.Page(pipeline.render, title="Pipeline", icon="💰",
                           url_path="pipeline"),
    "🧠 David Agent": st.Page(agent.render, title="David Agent", icon="🧠",
                              url_path="agent"),
    "📊 Översikt": st.Page(overview.render, title="Översikt", icon="📊",
                           url_path="oversikt"),
    "📥 Kontakter": st.Page(import_contacts.render, title="Kontakter",
                            icon="📥", url_path="kontakter"),
}


# ── Sidbyte begärt av en "Gå till"-knapp ─────────────────────────────────────
# shared.goto() sätter bara flaggan; bytet sker HÄR, i skriptets huvudflöde.
# st.switch_page() är en tyst no-op inuti en on_click-callback (den kastar
# inget fel — knappen ser bara ut att inte göra något), därför denna omväg.
_pending = st.session_state.pop("_goto", None)
if _pending in PAGES:
    st.switch_page(PAGES[_pending])


# ── Sidopanel: navigering + snabbstatistik ───────────────────────────────────
# st.navigation ritar ALLTID menyn högst upp i sidopanelen, oavsett var den
# anropas — en app-titel före den hamnar alltså under menyn och ser felplacerad
# ut. Titeln är därför borttagen: menyn är tillräcklig identitet, och
# webbläsarfliken säger redan "Sales pipeline - IHA".
nav = st.navigation(list(PAGES.values()))

# Ingen st.sidebar.divider() här — menyn ritar redan ett avslutande streck,
# och ytterligare ett gav två linjer med ett tomt glapp emellan.
st.sidebar.caption("Läget just nu")
try:
    # Cachad (45 s) — sidopanelen ritas om vid VARJE klick var som helst i
    # appen, så den okachade db.get_pipeline_stats() läste hela prospects-
    # tabellen varje gång. Cachen töms av shared.clear_data_cache() efter
    # varje skrivning, så siffrorna är ändå färska direkt efter en åtgärd.
    stats = shared.cached_pipeline_stats()
    st.sidebar.metric("Kontaktade", stats["kontaktade"])
    st.sidebar.metric("Möten bokade", stats["moten"])
    st.sidebar.metric("Konvertering", f"{stats['konvertering']}%")
except Exception:
    st.sidebar.caption("_(Anslut Supabase för statistik)_")


# ── Versionsmärke — så David direkt ser om senaste push är live ──────────────
def _version_label() -> str:
    import subprocess
    import datetime
    try:
        h = subprocess.check_output(
            ["git", "log", "-1", "--format=%h · %ad", "--date=format:%d %b %H:%M"],
            cwd=os.path.dirname(__file__), text=True, timeout=3).strip()
        if h:
            return h
    except Exception:
        pass
    try:  # fallback: när .git saknas i molnet ≈ tidpunkten koden checkades ut
        ts = os.path.getmtime(__file__)
        return "utcheckad " + datetime.datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
    except Exception:
        return ""


_v = _version_label()
if _v:
    st.sidebar.caption(f"🔖 Version: {_v}")


# ── Rita vald sida ───────────────────────────────────────────────────────────
# nav.run() kör render()-funktionen för den sida menyn står på.

nav.run()
