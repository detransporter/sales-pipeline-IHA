"""📊 Översikt — pipeline, statistik och inlärning (verktyg)."""

import pandas as pd
import streamlit as st

from agents import learning
from agents.qualifier import qualify_reply
from database import supabase_client as db
from views.shared import PIPELINE_STATUSES


def render():
    st.title("📊 Översikt")

    try:
        stats = db.get_pipeline_stats()
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
    _sf = st.session_state.get("ov_status_v", _status_opts[0])
    _ms = st.session_state.get("ov_minscore_v", 0)
    col1, col2 = st.columns(2)
    with col1:
        status_filter = st.selectbox(
            "Status", _status_opts,
            index=_status_opts.index(_sf) if _sf in _status_opts else 0,
            key="ov_status")
    with col2:
        min_score = st.slider("Min. score", 0, 20, _ms, key="ov_minscore")
    st.session_state["ov_status_v"] = status_filter
    st.session_state["ov_minscore_v"] = min_score

    try:
        status_param = None if status_filter == "Alla" else status_filter
        prospects = db.get_prospects(status=status_param, min_score=min_score)
    except Exception as e:
        st.error(f"Fel: {e}")
        prospects = []

    if not prospects:
        st.info("Inga kontakter matchar filtret.")
    else:
        df = pd.DataFrame(prospects)
        display_cols = [c for c in ["namn", "titel", "bolag", "bransch", "score", "status",
                                    "created_at"] if c in df.columns]
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("Redigera kontakt")
        prospect_labels = {f"{p['namn']} — {p['bolag']}": p for p in prospects}
        chosen_label = st.selectbox("Välj kontakt att redigera",
                                    list(prospect_labels.keys()), key="edit_pick")
        chosen = prospect_labels[chosen_label]

        tab_edit, tab_status, tab_delete = st.tabs(["✏️ Uppgifter", "🔄 Status", "🗑️ Ta bort"])

        with tab_edit:
            with st.form("edit_prospect"):
                r1c1, r1c2 = st.columns(2)
                e_namn   = r1c1.text_input("Namn",    value=chosen.get("namn", ""))
                e_titel  = r1c2.text_input("Roll/titel", value=chosen.get("titel", ""))
                r2c1, r2c2 = st.columns(2)
                e_bolag  = r2c1.text_input("Bolag",   value=chosen.get("bolag", ""))
                e_bransch= r2c2.text_input("Bransch", value=chosen.get("bransch", ""))
                r3c1, r3c2 = st.columns(2)
                e_email  = r3c1.text_input("E-post",  value=chosen.get("email", ""))
                e_li     = r3c2.text_input("LinkedIn-URL", value=chosen.get("linkedin_url", ""))
                e_website= st.text_input("Hemsida",   value=chosen.get("website", ""))
                if st.form_submit_button("💾 Spara ändringar", type="primary"):
                    try:
                        fields = {
                            "namn": e_namn.strip(),
                            "titel": e_titel.strip(),
                            "bolag": e_bolag.strip(),
                            "bransch": e_bransch.strip(),
                            "email": e_email.strip(),
                            "linkedin_url": e_li.strip(),
                            "website": e_website.strip(),
                        }
                        db.update_prospect(chosen["id"], {k: v for k, v in fields.items() if v})
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

        with tab_delete:
            st.warning(f"Du håller på att ta bort **{chosen.get('namn')} — "
                       f"{chosen.get('bolag')}** permanent. Det går inte att ångra.")
            confirm = st.checkbox("Ja, jag är säker — ta bort kontakten")
            if st.button("🗑️ Ta bort kontakt", type="primary", key="delete_prospect",
                         disabled=not confirm):
                try:
                    db.delete_prospect(chosen["id"])
                    st.success("Kontakten är borttagen.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

    st.divider()
    with st.expander("📈 Vad funkar? (inlärning från historiken)"):
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
        try:
            sent = db.get_sent_emails(limit=100)
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
        try:
            notes = db.list_memory(limit=10)
            if notes:
                for n in notes:
                    st.markdown(f"**{(n.get('created_at') or '')[:10]}**")
                    st.caption(n.get("content", ""))
            else:
                st.caption("Minnet är tomt ännu.")
        except Exception as e:
            st.caption(f"Kunde inte läsa minnet: {e}")
