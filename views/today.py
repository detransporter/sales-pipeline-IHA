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

    # ── Din aktivitet: hur många du kontaktar per dag ────────────────────────
    _render_activity()

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


DAILY_GOAL = 20  # mål: antal kontakter per dag (nya, uppföljningar eller samtal)


def _render_activity(days: int = 14):
    """Visa hur många kontakter du gjort per dag — mät din dagliga aktivitet."""
    try:
        data = db.get_daily_activity(days=days)
    except Exception as e:
        # Sänk aldrig startsidan — men visa en ledtråd om något gick fel.
        st.divider()
        st.markdown("#### 📈 Din aktivitet")
        st.caption(f"Kunde inte läsa aktivitet just nu: {e}")
        return
    if not data:
        return

    import pandas as pd

    today_n = data[-1]["antal"]
    week_n = sum(d["antal"] for d in data[-7:])
    active_days = sum(1 for d in data[-7:] if d["antal"] > 0)
    snitt = round(week_n / 7, 1)
    reached = today_n >= DAILY_GOAL

    st.divider()
    st.markdown("#### 📈 Din aktivitet")
    c1, c2, c3 = st.columns(3)
    c1.metric("Kontaktade idag", f"{today_n} / {DAILY_GOAL}",
              delta=("✅ Mål nått!" if reached else f"{DAILY_GOAL - today_n} kvar"),
              delta_color=("normal" if reached else "off"))
    c2.metric("Senaste 7 dagarna", week_n, help=f"Aktiv {active_days} av 7 dagar")
    c3.metric("Snitt/dag (7 dgr)", snitt)

    # Framstegsmätare mot dagens mål.
    st.progress(min(today_n / DAILY_GOAL, 1.0))
    if reached:
        st.success(f"🎯 Dagens mål på {DAILY_GOAL} kontakter är nått — starkt jobbat!")
    else:
        st.caption(f"🎯 Mål: {DAILY_GOAL} kontakter/dag · "
                   f"**{DAILY_GOAL - today_n} kvar** idag.")

    # Graf med mållinje — gröna staplar = dagar då målet nåddes.
    df = pd.DataFrame(data)
    df["dag"] = pd.to_datetime(df["datum"]).dt.strftime("%a %d/%m")
    colors = ["#22c55e" if n >= DAILY_GOAL else "#94a3b8" for n in df["antal"]]
    try:
        import plotly.graph_objects as go
        fig = go.Figure(go.Bar(x=df["dag"], y=df["antal"], marker_color=colors,
                               text=df["antal"], textposition="outside"))
        fig.add_hline(y=DAILY_GOAL, line_dash="dash", line_color="#ef4444",
                      annotation_text=f"Mål {DAILY_GOAL}", annotation_position="top left")
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=220,
                          yaxis_title=None, xaxis_title=None, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        # Faller tillbaka på enkel graf om plotly saknas.
        st.bar_chart(df.set_index("dag")["antal"], height=200, color="#22c55e")
    st.caption(f"Antal utgående kontakter per dag (mejl, DM, uppföljning, samtal) mot "
               f"målet {DAILY_GOAL}/dag — senaste {days} dagarna. "
               "Grön stapel = mål nått.")


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
