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

from database import supabase_client as db
from views import (today, find_companies, leads, replies, meetings, overview,
                   import_contacts)

st.set_page_config(
    page_title="Sales pipeline - IHA",
    page_icon="📊",
    layout="wide",
)


# ── Sidor: namn i menyn → funktionen som ritar sidan ─────────────────────────
# Ordningen här är ordningen i menyn (tratten uppifrån och ner + verktyg sist).
PAGES = {
    "🏠 Idag": today.render,
    "🔍 Hitta bolag": find_companies.render,
    "🌱 Leads": leads.render,
    "💬 Svar & uppföljning": replies.render,
    "📅 Möten": meetings.render,
    "📊 Översikt": overview.render,
    "📥 Importera kontakter": import_contacts.render,
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
