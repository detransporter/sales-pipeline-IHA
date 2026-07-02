"""📅 Möten — bokade möten + boka nytt."""

from datetime import date

import streamlit as st

from database import supabase_client as db


def render():
    st.title("📅 Möten")

    tab1, tab2 = st.tabs(["📋 Bokade möten", "➕ Boka nytt möte"])

    with tab1:
        try:
            meetings = db.get_meetings()
        except Exception as e:
            st.error(f"Fel: {e}")
            meetings = []

        if not meetings:
            st.info("Inga bokade möten.")
        else:
            for m in meetings:
                prospect_name = m.get("prospects", {}).get("namn", "Okänd") if m.get("prospects") else "Okänd"
                bolag = m.get("prospects", {}).get("bolag", "") if m.get("prospects") else ""
                with st.expander(f"{m['datum']} — {prospect_name} @ {bolag} [{m['status']}]"):
                    notes = st.text_area("Anteckningar", value=m.get("anteckningar") or "", key=f"notes_{m['id']}")
                    new_status = st.selectbox(
                        "Status",
                        ["bokad", "genomford", "avbokad"],
                        index=["bokad", "genomford", "avbokad"].index(m["status"]),
                        key=f"mstatus_{m['id']}",
                    )
                    if st.button("💾 Spara", key=f"save_meeting_{m['id']}"):
                        try:
                            db.update_meeting(m["id"], {"anteckningar": notes, "status": new_status})
                            st.success("Sparat!")
                        except Exception as e:
                            st.error(f"Fel: {e}")

    with tab2:
        st.subheader("Boka nytt möte")
        try:
            prospects_all = db.get_prospects()
            prospect_options = {f"{p['namn']} — {p['bolag']}": p for p in prospects_all}
        except Exception as e:
            st.error(f"Fel: {e}")
            prospect_options = {}

        if prospect_options:
            chosen_p = st.selectbox("Kontakt", list(prospect_options.keys()))
            meeting_date = st.date_input("Datum", value=date.today())
            if st.button("📅 Boka möte", type="primary"):
                try:
                    p = prospect_options[chosen_p]
                    db.insert_meeting(p["id"], meeting_date.isoformat())
                    db.update_prospect_status(p["id"], "mote_bokat")
                    st.success(f"Möte bokat med {p['namn']} den {meeting_date}!")
                except Exception as e:
                    st.error(f"Fel: {e}")
