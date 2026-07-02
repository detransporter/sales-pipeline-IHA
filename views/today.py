"""🏠 Idag — startsida: visar vad som behöver göras och länkar dit."""

import streamlit as st

from agents import orchestrator
from database import supabase_client as db
from views.shared import goto


def render():
    st.title("🏠 Idag")
    st.caption("Din dag i ordning. Varje kort visar vad som väntar — klicka för att gå dit.")

    try:
        state = orchestrator.gather_state()
    except Exception as e:
        st.error(f"Kunde inte läsa läget: {e}")
        st.stop()

    try:
        replies = db.get_inbox_replies(handled=False)
    except Exception:
        replies = []

    n_leads = len(state.get("pending_leads", []))
    n_replies = len(replies)
    n_followups = len(state.get("followups", [])) + len(state.get("closes", []))
    n_meetings = len(state.get("summary", {}).get("meetings_today", []))

    # ── Nästa steg: den enda viktigaste saken just nu, rankad på brådska ──────
    _render_next_step(n_meetings, n_replies, n_followups, n_leads)
    st.divider()
    st.caption("Hela dagen i översikt:")

    # Steg-korten i tratt-ordning
    steps = [
        ("🔍", "Hitta bolag", "Sök fram nya bolag att kontakta", None, "🔍 Hitta bolag",
         "Öppna sök"),
        ("🌱", "Leads att godkänna", "Hitta person + godkänn", n_leads, "🌱 Leads",
         "Hantera leads"),
        ("💬", "Svar att hantera", "Svara på inkomna LinkedIn-svar", n_replies,
         "💬 Svar & uppföljning", "Öppna svar"),
        ("🔔", "Uppföljningar", "Följ upp / stäng kontakter", n_followups,
         "💬 Svar & uppföljning", "Öppna uppföljningar"),
        ("📅", "Möten idag", "Förbered dagens möten", n_meetings, "📅 Möten", "Öppna möten"),
    ]

    cols = st.columns(3)
    for i, (icon, titel, beskr, antal, target, knapp) in enumerate(steps):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"### {icon} {titel}")
                if antal is not None:
                    st.metric(label=beskr, value=antal)
                else:
                    st.caption(beskr)
                st.button(f"{knapp} →", key=f"go_{i}", on_click=goto, args=(target,),
                          use_container_width=True)

    st.divider()
    st.caption("Tips: följ korten i ordning — hitta bolag → godkänn leads → skicka DM → "
               "hantera svar → följ upp → boka möte.")


def _render_next_step(n_meetings, n_replies, n_followups, n_leads):
    """
    Lyft fram EN sak att göra just nu, rankad på brådska:
    möte idag > svar > uppföljning > godkänn leads > fyll på tratten.
    """
    if n_meetings:
        icon, rubrik, skäl, target, knapp = (
            "📅", f"Du har {n_meetings} möte(n) idag",
            "Förbered dagens möten så du går in påläst.", "📅 Möten", "Öppna möten")
    elif n_replies:
        icon, rubrik, skäl, target, knapp = (
            "💬", f"{n_replies} svar väntar på dig",
            "Någon har svarat — svara snabbt medan intresset är varmt.",
            "💬 Svar & uppföljning", "Hantera svar")
    elif n_followups:
        icon, rubrik, skäl, target, knapp = (
            "🔔", f"{n_followups} uppföljningar att göra",
            "Kontakter som väntat klart — följ upp innan de svalnar.",
            "💬 Svar & uppföljning", "Öppna uppföljningar")
    elif n_leads:
        icon, rubrik, skäl, target, knapp = (
            "🌱", f"{n_leads} leads att godkänna",
            "Hitta person och godkänn för att bygga pipeline.", "🌱 Leads", "Hantera leads")
    else:
        icon, rubrik, skäl, target, knapp = (
            "🔍", "Allt är hanterat — fyll på tratten",
            "Inget väntar. Sök fram nya bolag med bundet lagerkapital.",
            "🔍 Hitta bolag", "Hitta bolag")

    with st.container(border=True):
        st.markdown(f"#### 👉 Nästa steg: {icon} {rubrik}")
        st.caption(skäl)
        st.button(f"{knapp} →", key="next_step", on_click=goto, args=(target,),
                  type="primary")
