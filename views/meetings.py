"""📅 Möten — bokade möten, kalendervy + boka nytt."""

from datetime import date

import streamlit as st

from database import supabase_client as db
from views.shared import kategori_label, unique_prospect_labels

_MEETING_STATUSES = ["bokad", "genomford", "avbokad"]
# Färg per status i kalendern — samma statusar som redigeringsformuläret.
_STATUS_COLOR = {"bokad": "#2563eb", "genomford": "#16a34a", "avbokad": "#dc2626"}


def render():
    st.title("📅 Möten")

    try:
        meetings = db.get_meetings()
    except Exception as e:
        st.error(f"Fel: {e}")
        meetings = []

    tab_cal, tab_list, tab_new = st.tabs(
        ["🗓️ Kalender", "📋 Bokade möten", "➕ Boka nytt möte"])

    with tab_cal:
        _render_calendar_tab(meetings)

    with tab_list:
        _render_list_tab(meetings)

    with tab_new:
        _render_new_meeting_tab()


def _render_calendar_tab(meetings: list) -> None:
    # Tålig import — ett mindre community-paket med egen JS-frontend. Skulle
    # det någonsin misslyckas installeras på Streamlit Cloud ska bara den här
    # fliken tappa kalendervyn, inte hela appen (app.py importerar alla sidor
    # direkt vid start, så en trasig modulimport här skulle annars slagit ut
    # ALLA sidor, inte bara Möten).
    try:
        from streamlit_calendar import calendar as st_calendar
    except Exception as e:
        st.warning(f"Kalenderkomponenten kunde inte laddas ({e}). "
                   "Använd 📋 Bokade möten-fliken istället.")
        return

    if not meetings:
        st.info("Inga bokade möten.")
        return

    events = []
    for m in meetings:
        _p = m.get("prospects") or {}
        namn = _p.get("namn") or "Okänd"
        bolag = _p.get("bolag") or ""
        events.append({
            "id": str(m["id"]),
            "title": f"{namn} @ {bolag}" if bolag else namn,
            "start": m["datum"],
            "allDay": True,
            "color": _STATUS_COLOR.get(m["status"], "#2563eb"),
            # extendedProps är FullCalendars kanal för egen data på ett event —
            # så vi kan hitta rätt möte i `meetings` igen när kortet klickas.
            "extendedProps": {"meeting_id": m["id"]},
        })

    st.caption("Klicka på ett möte för att läsa/ändra anteckningar och status.")
    result = st_calendar(
        events=events,
        options={
            "initialView": "dayGridMonth",
            "locale": "sv",
            "firstDay": 1,  # veckan börjar på måndag
            "headerToolbar": {"left": "prev,next today",
                              "center": "title",
                              "right": "dayGridMonth,listMonth"},
            "height": 650,
        },
        key="meetings_calendar",
    )

    # Klick-callbacken sätter ett värde som ligger kvar tills nästa klick —
    # spara VILKET möte som senast klickats i session_state så redigeraren
    # nedan visas konsekvent, oavsett vad som orsakade den senaste omritningen.
    if result and result.get("callback") == "eventClick":
        clicked = result["eventClick"]["event"]["extendedProps"].get("meeting_id")
        st.session_state["cal_selected_meeting"] = clicked

    selected_id = st.session_state.get("cal_selected_meeting")
    if selected_id is not None:
        chosen = next((m for m in meetings if m["id"] == selected_id), None)
        if chosen:
            st.divider()
            _render_meeting_editor(chosen, key_prefix="cal")


def _render_list_tab(meetings: list) -> None:
    if not meetings:
        st.info("Inga bokade möten.")
        return
    for m in meetings:
        _p = m.get("prospects") or {}
        prospect_name = _p.get("namn") or "Okänd"
        bolag = _p.get("bolag") or ""
        _kb = kategori_label(_p.get("kategori"))
        _pre = f"{_kb} · " if _kb else ""
        with st.expander(f"{m['datum']} — {_pre}{prospect_name} @ {bolag} [{m['status']}]"):
            _render_meeting_editor(m, key_prefix="list")


def _render_meeting_editor(m: dict, key_prefix: str) -> None:
    """
    Redigera anteckningar/status för ETT möte. Delas mellan kalender- och
    listvyn — men båda flikarnas innehåll renderas alltid av Streamlit
    (st.tabs styr bara vilken som SYNS, inte vilken som körs), så samma
    möte kan behöva ritas upp i båda samtidigt. `key_prefix` håller
    widget-nycklarna unika mellan de två anropsställena — annars kraschar
    Streamlit med "duplicate widget key" så fort ett möte råkar vara valt
    i kalendern samtidigt som det står i listan (dvs. alltid).
    """
    notes = st.text_area("Anteckningar", value=m.get("anteckningar") or "",
                         key=f"{key_prefix}_notes_{m['id']}")
    new_status = st.selectbox(
        "Status", _MEETING_STATUSES,
        # Skydd mot okänt statusvärde (manuell DB-ändring, framtida
        # migrering) — samma mönster som pipeline.py/overview.py.
        index=_MEETING_STATUSES.index(m["status"]) if m["status"] in _MEETING_STATUSES else 0,
        key=f"{key_prefix}_mstatus_{m['id']}",
    )
    if st.button("💾 Spara", key=f"{key_prefix}_save_meeting_{m['id']}"):
        try:
            db.update_meeting(m["id"], {"anteckningar": notes, "status": new_status})
            st.success("Sparat!")
        except Exception as e:
            st.error(f"Fel: {e}")


def _render_new_meeting_tab() -> None:
    st.subheader("Boka nytt möte")
    try:
        prospects_all = db.get_prospects()
        prospect_options = unique_prospect_labels(prospects_all)
    except Exception as e:
        st.error(f"Fel: {e}")
        prospect_options = {}

    if prospect_options:
        chosen_p = st.selectbox("Kontakt", list(prospect_options.keys()))
        meeting_date = st.date_input("Datum", value=date.today())
        new_notes = st.text_area(
            "Anteckningar (valfritt)", key="new_meeting_notes",
            placeholder="T.ex. vad mötet ska handla om, eller vad du vill ha med dig in.",
            help="Går att ändra senare via kalendern eller listan — men bekvämt "
                 "att skriva direkt medan du kommer ihåg det.")
        if st.button("📅 Boka möte", type="primary"):
            try:
                p = prospect_options[chosen_p]
                db.insert_meeting(p["id"], meeting_date.isoformat(), anteckningar=new_notes)
                db.update_prospect_status(p["id"], "mote_bokat",
                                          meeting_date=meeting_date.isoformat())
                st.success(f"Möte bokat med {p['namn']} den {meeting_date}!")
            except Exception as e:
                st.error(f"Fel: {e}")
