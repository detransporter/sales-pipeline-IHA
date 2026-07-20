"""📊 Översikt — pipeline, statistik och inlärning (verktyg)."""

import pandas as pd
import streamlit as st

from datetime import date, timedelta

from agents import learning
from agents.followup import postpone_followup
from agents.qualifier import qualify_reply
from database import supabase_client as db
from views.shared import (PIPELINE_STATUSES, KONTAKT_KATEGORIER,
                          cached_prospects, cached_pipeline_stats, cached_sent_emails,
                          clear_data_cache, unique_prospect_labels)

# Vilket uppföljningssteg en kontakt är på väg mot, givet nuvarande status.
# Styr tröskeln när kontakten dyker upp igen efter en uppskjutning.
_NEXT_ACTION = {
    "skickad": "followup_1",
    "followup_1": "followup_2",
    "followup_2": "close",
}


def render():
    st.title("📊 Översikt")

    try:
        stats = cached_pipeline_stats()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Totalt kontaktade", stats["kontaktade"])
        c2.metric("Fått svar", stats["svar"])
        c3.metric("Möten bokade", stats["moten"])
        c4.metric("Konvertering", f"{stats['konvertering']}%")
    except Exception as e:
        st.error(f"Supabase-fel: {e}")

    st.divider()
    st.subheader("Pipeline")
    # Kom ihåg val mellan sidbyten (Streamlit rensar widget-state för sidor
    # som inte visas — spegla därför till egna _v-nycklar).
    _status_opts = ["Alla"] + PIPELINE_STATUSES
    _kat_opts = ["Alla"] + KONTAKT_KATEGORIER
    _sf = st.session_state.get("ov_status_v", _status_opts[0])
    _kf = st.session_state.get("ov_kategori_v", _kat_opts[0])
    _ms = st.session_state.get("ov_minscore_v", 0)
    col1, col2, col3 = st.columns(3)
    with col1:
        status_filter = st.selectbox(
            "Status", _status_opts,
            index=_status_opts.index(_sf) if _sf in _status_opts else 0,
            key="ov_status")
    with col2:
        kat_filter = st.selectbox(
            "Kategori", _kat_opts,
            index=_kat_opts.index(_kf) if _kf in _kat_opts else 0,
            key="ov_kategori")
    with col3:
        min_score = st.slider("Min. score", 0, 20, _ms, key="ov_minscore")
    st.session_state["ov_status_v"] = status_filter
    st.session_state["ov_kategori_v"] = kat_filter
    st.session_state["ov_minscore_v"] = min_score

    try:
        status_param = None if status_filter == "Alla" else status_filter
        prospects = cached_prospects(status=status_param, min_score=min_score)
        if kat_filter != "Alla":
            prospects = [p for p in prospects if (p.get("kategori") or "") == kat_filter]
    except Exception as e:
        st.error(f"Fel: {e}")
        prospects = []

    if not prospects:
        st.info("Inga kontakter matchar filtret.")
    else:
        df = pd.DataFrame(prospects)
        display_cols = [c for c in ["kategori", "namn", "titel", "bolag", "bransch",
                                    "score", "status", "created_at"] if c in df.columns]
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("Redigera kontakt")
        prospect_labels = unique_prospect_labels(prospects)
        chosen_label = st.selectbox("Välj kontakt att redigera",
                                    list(prospect_labels.keys()), key="edit_pick")
        chosen = prospect_labels[chosen_label]

        tab_edit, tab_status, tab_snooze, tab_delete = st.tabs(
            ["✏️ Uppgifter", "🔄 Status", "📅 Skjut upp", "🗑️ Ta bort"])

        with tab_edit:
            with st.form("edit_prospect"):
                r1c1, r1c2 = st.columns(2)
                # .get(key, "") ger None om kolumnen är null → text_input returnerar
                # None och .strip() nedan kraschar. Tvinga str med `or ""`.
                e_namn   = r1c1.text_input("Namn",    value=chosen.get("namn") or "")
                e_titel  = r1c2.text_input("Roll/titel", value=chosen.get("titel") or "")
                r2c1, r2c2 = st.columns(2)
                e_bolag  = r2c1.text_input("Bolag",   value=chosen.get("bolag") or "")
                e_bransch= r2c2.text_input("Bransch", value=chosen.get("bransch") or "")
                r3c1, r3c2 = st.columns(2)
                e_email  = r3c1.text_input("E-post",  value=chosen.get("email") or "")
                e_li     = r3c2.text_input("LinkedIn-URL", value=chosen.get("linkedin_url") or "")
                e_website= st.text_input("Hemsida",   value=chosen.get("website") or "")
                _cur_kat = chosen.get("kategori") or KONTAKT_KATEGORIER[0]
                e_kategori = st.selectbox(
                    "Kategori", KONTAKT_KATEGORIER,
                    index=(KONTAKT_KATEGORIER.index(_cur_kat)
                           if _cur_kat in KONTAKT_KATEGORIER else 0))
                if st.form_submit_button("💾 Spara ändringar", type="primary"):
                    _namn_ny = e_namn.strip()
                    if not _namn_ny:
                        st.error("Namn kan inte vara tomt.")
                    else:
                        try:
                            fields = {
                                "namn": _namn_ny,
                                "titel": e_titel.strip(),
                                "bolag": e_bolag.strip(),
                                "bransch": e_bransch.strip(),
                                "email": e_email.strip(),
                                "linkedin_url": e_li.strip(),
                                "website": e_website.strip(),
                                "kategori": e_kategori,
                            }
                            # Skicka ALLA fält, även de som rensats till tomt sträng.
                            # Tidigare filtrerades tomma värden bort ({k:v if v}) så
                            # ett fält gick att SÄTTA men aldrig RENSA — det gamla
                            # (felaktiga) värdet låg kvar i databasen permanent.
                            db.update_prospect(chosen["id"], fields)
                            clear_data_cache()
                            st.success("Sparat!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")

        with tab_status:
            cur = chosen.get("status", "ej_kontaktad")
            new_status = st.selectbox(
                "Ny status", PIPELINE_STATUSES,
                index=PIPELINE_STATUSES.index(cur) if cur in PIPELINE_STATUSES else 0,
                key="status_pick",
            )
            svar_text = ""
            if new_status in ("svar_ja", "svar_nej", "mote_bokat"):
                svar_text = st.text_area("Klistra in svaret (för AI-analys, valfritt)")
            if st.button("💾 Uppdatera status", type="primary", key="update_status"):
                try:
                    db.update_prospect_status(chosen["id"], new_status)
                    clear_data_cache()
                    if svar_text.strip():
                        analysis = qualify_reply(svar_text)
                        st.info(
                            f"**Kategori:** {analysis['kategori']}\n\n"
                            f"**Nästa steg:** {analysis['nästa_steg']}\n\n"
                            f"**Förslag på svar:** {analysis['förslag_svar']}"
                        )
                    st.success("Status uppdaterad!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

        with tab_snooze:
            cur = chosen.get("status", "ej_kontaktad")
            action = _NEXT_ACTION.get(cur)
            if not action:
                st.info("Skjut upp gäller kontakter som redan är kontaktade och väntar "
                        "på uppföljning (status *skickad*, *followup_1* eller *followup_2*). "
                        f"Den här kontakten har status **{cur}**.")
            else:
                st.caption("Mottagaren på semester? Flytta fram nästa kontakt så att "
                           "bolaget försvinner ur uppföljningsflödet och dyker upp igen "
                           "den dag du väljer.")
                sc1, sc2, sc3 = st.columns(3)
                quick = None
                if sc1.button("+1 vecka", key="ov_pp1", use_container_width=True):
                    quick = date.today() + timedelta(days=7)
                if sc2.button("+2 veckor", key="ov_pp2", use_container_width=True):
                    quick = date.today() + timedelta(days=14)
                if sc3.button("Efter 15 aug", key="ov_pp3", use_container_width=True):
                    quick = max(date.today() + timedelta(days=1),
                                date(date.today().year, 8, 15))
                valt = st.date_input("…eller välj datum",
                                     value=date.today() + timedelta(days=14),
                                     min_value=date.today() + timedelta(days=1),
                                     key="ov_pp_date")
                do_it = st.button("📅 Skjut upp till valt datum", key="ov_pp_go",
                                  type="primary")
                target = quick or (valt if do_it else None)
                if target:
                    try:
                        postpone_followup(chosen["id"], action, target)
                        clear_data_cache()
                        st.success(f"✅ Uppskjuten — {chosen.get('bolag','kontakten')} "
                                   f"dyker upp igen {target.isoformat()}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")

        with tab_delete:
            st.warning(f"Du håller på att ta bort **{chosen.get('namn')} — "
                       f"{chosen.get('bolag')}** permanent. Det går inte att ångra.")
            confirm = st.checkbox("Ja, jag är säker — ta bort kontakten")
            if st.button("🗑️ Ta bort kontakt", type="primary", key="delete_prospect",
                         disabled=not confirm):
                try:
                    db.delete_prospect(chosen["id"])
                    clear_data_cache()
                    st.success("Kontakten är borttagen.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

    st.divider()
    st.caption("Tunga rutor laddas först när du öppnar dem — håller sidbytet snabbt.")

    # Latladdade rutor: DB-anropet körs först när du trycker Ladda (annars skulle
    # de köras vid varje omladdning även hopfällda och göra sidan seg).
    with st.expander("📈 Vad funkar? (inlärning från historiken)"):
        if st.button("Ladda", key="load_learning"):
            st.session_state["ov_show_learning"] = True
        if st.session_state.get("ov_show_learning"):
            try:
                insight = learning.analyze_what_works()
                st.write(insight["brief"])
                if insight["angle_stats"]:
                    st.caption("Per vinkel:")
                    st.dataframe(pd.DataFrame([
                        {"vinkel": k, **v} for k, v in insight["angle_stats"].items()
                    ]), use_container_width=True, hide_index=True)
            except Exception as e:
                st.caption(f"Ingen inlärningsdata ännu: {e}")

    with st.expander("📧 Skickade mejl (logg)"):
        if st.button("Ladda", key="load_sent"):
            st.session_state["ov_show_sent"] = True
        if st.session_state.get("ov_show_sent"):
            try:
                sent = cached_sent_emails(limit=100)
                if sent:
                    rows = []
                    for m in sent:
                        pr = m.get("prospects") or {}
                        msg = m.get("meddelande", "") or ""
                        amne = ""
                        for line in msg.splitlines():
                            if line.lower().startswith("ämne:"):
                                amne = line.split(":", 1)[1].strip()
                                break
                        rows.append({
                            "Skickat": (m.get("skickad_at") or "")[:16].replace("T", " "),
                            "Kontakt": pr.get("namn", ""),
                            "Bolag": pr.get("bolag", ""),
                            "Ämne": amne,
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("Inga mejl skickade ännu.")
            except Exception as e:
                st.caption(f"Kunde inte läsa mejlloggen: {e}")

    with st.expander("🧠 Minne (senaste noteringar)"):
        if st.button("Ladda", key="load_memory"):
            st.session_state["ov_show_memory"] = True
        if st.session_state.get("ov_show_memory"):
            try:
                from brain import open_brain
                notes = open_brain.list_thoughts(limit=10)
                st.caption(notes if notes else "Minnet är tomt ännu.")
            except Exception as e:
                st.caption(f"Kunde inte läsa minnet: {e}")
