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

from database import supabase_client as db
from views import (today, find_companies, leads, replies, meetings, pipeline,
                   agent, overview, import_contacts)

st.set_page_config(
    page_title="Sales pipeline - IHA",
    page_icon="📊",
    layout="wide",
)


# ── Inloggning ───────────────────────────────────────────────────────────────
# Aktiveras bara om APP_PASSWORD är satt (dvs online). Lokalt utan lösenord är
# appen öppen. På Streamlit Cloud skyddar detta appen även om länken är publik.
def _require_login() -> bool:
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw or st.session_state.get("_authed"):
        return True
    st.title("🔒 Sales pipeline - IHA")
    st.caption("Logga in för att fortsätta.")
    entered = st.text_input("Lösenord", type="password")
    if entered:
        if entered == pw:
            st.session_state["_authed"] = True
            st.rerun()
        st.error("Fel lösenord.")
    return False


if not _require_login():
    st.stop()


# ── Sidor: namn i menyn → funktionen som ritar sidan ─────────────────────────
# Ordningen här är ordningen i menyn (tratten uppifrån och ner + verktyg sist).
PAGES = {
    "🏠 Idag": today.render,
    "🔍 Hitta bolag": find_companies.render,
    "🌱 Leads": leads.render,
    "💬 Svar & uppföljning": replies.render,
    "📅 Möten": meetings.render,
    "💰 Pipeline": pipeline.render,
    "🧠 David Agent": agent.render,
    "📊 Översikt": overview.render,
    "📥 Kontakter": import_contacts.render,
}


# ── Sidopanel: navigering + snabbstatistik ───────────────────────────────────

st.sidebar.title("📊 Sales pipeline - IHA")
st.sidebar.divider()
st.sidebar.caption("Arbetsflöde — uppifrån och ner")
page = st.sidebar.radio("Navigation", list(PAGES.keys()), key="nav",
                        label_visibility="collapsed")

st.sidebar.divider()
try:
    stats = db.get_pipeline_stats()
    st.sidebar.metric("Kontaktade", stats["kontaktade"])
    st.sidebar.metric("Möten bokade", stats["moten"])
    st.sidebar.metric("Konvertering", f"{stats['konvertering']}%")
except Exception:
    st.sidebar.caption("_(Anslut Supabase för statistik)_")


# ── Rita vald sida ───────────────────────────────────────────────────────────

PAGES[page]()
