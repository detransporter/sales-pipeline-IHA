"""💬 Svar & uppföljning — inkomna svar + uppföljningar (sammanslaget)."""

import streamlit as st

from agents import inbox_watcher
from agents.followup import get_followups_due, process_close
from database import supabase_client as db
from views.shared import person_link_inline


def render():
    st.title("💬 Svar & uppföljning")

    tab_svar, tab_followup = st.tabs(["💬 Svar att hantera", "🔔 Uppföljningar"])

    with tab_svar:
        _render_replies_tab()

    with tab_followup:
        _render_followups_tab()


def _render_replies_tab():
    st.caption("Fick du svar på LinkedIn? Klistra in det — agenten kvalificerar och "
               "skriver ditt nästa meddelande. Den skickar aldrig något själv.")

    with st.container(border=True):
        st.markdown("### ➕ Klistra in ett svar")
        try:
            all_p = db.get_prospects()
        except Exception as e:
            all_p = []
            st.error(f"Kunde inte hämta kontakter: {e}")
        if all_p:
            opt = {f"{p['namn']} — {p.get('bolag','')}": p["id"] for p in all_p}
            chosen = st.selectbox("Kontakt", list(opt.keys()), key="manual_reply_contact")
            reply_text = st.text_area("Vad de skrev", key="manual_reply_text", height=90)
            if st.button("🤖 Behandla svaret", type="primary"):
                if not reply_text.strip():
                    st.warning("Klistra in svarstexten först.")
                else:
                    with st.spinner("Kvalificerar och skriver förslag..."):
                        try:
                            inbox_watcher.process_manual_reply(opt[chosen], reply_text)
                            st.success("Klart! Förslaget ligger i kön nedan. 👇")
                        except Exception as e:
                            msg = str(e)
                            if "row-level security" in msg:
                                st.error("Databasen blockerar skrivning (RLS). Kör i Supabase SQL Editor: "
                                         "`ALTER TABLE inbox_replies DISABLE ROW LEVEL SECURITY;`")
                            else:
                                st.error(f"Fel: {e}")

    # ── Email-svar (IMAP) ──────────────────────────────────────────────
    from integrations import email_inbox as _email_inbox
    if _email_inbox.is_configured():
        with st.container(border=True):
            st.markdown("### 📧 Email-svar")
            if st.button("🔄 Hämta olästa email-svar", key="fetch_email_replies"):
                with st.spinner("Ansluter till Gmail..."):
                    try:
                        # Bygg addr → prospect_id från skickade mejl
                        sent = db.get_sent_emails(limit=200)
                        addr_to_pid: dict[str, str] = {}
                        for m in sent:
                            msg_text = m.get("meddelande", "") or ""
                            for line in msg_text.splitlines():
                                if line.lower().startswith("till:"):
                                    addr = line.split(":", 1)[1].strip().lower()
                                    if addr and m.get("prospect_id"):
                                        addr_to_pid[addr] = m["prospect_id"]
                                    break
                        replies = _email_inbox.fetch_unread_replies(
                            known_addresses=set(addr_to_pid.keys()) or None
                        )
                        # Koppla prospect_id, filtrera sedan bort redan behandlade
                        matched = [
                            {**r, "prospect_id": addr_to_pid.get(r["from_addr"])}
                            for r in replies
                        ]
                        fresh = [
                            r for r in matched
                            if not db.reply_exists(
                                r["message_id"], r.get("prospect_id"), r["body"]
                            )
                        ]
                        st.session_state["pending_email_replies"] = fresh
                        if fresh:
                            st.success(f"{len(fresh)} nya email-svar hittade.")
                        else:
                            st.info("Inga nya olästa email-svar från kända kontakter.")
                    except Exception as e:
                        st.error(f"Kunde inte hämta email-svar: {e}")

            pending_email = st.session_state.get("pending_email_replies") or []
            for i, r in enumerate(pending_email):
                pid = r.get("prospect_id")
                with st.container(border=True):
                    col_info, col_btn = st.columns([4, 1])
                    with col_info:
                        st.markdown(f"**{r['from_name'] or r['from_addr']}** — {r['subject']}")
                        st.caption(r["date"])
                        st.text_area("Svar (citatfritt)", value=r["body"], height=100,
                                     key=f"email_reply_body_{i}", disabled=True)
                    with col_btn:
                        if pid:
                            if st.button("🤖 Behandla", key=f"proc_email_{i}", type="primary"):
                                with st.spinner("Kvalificerar..."):
                                    try:
                                        inbox_watcher.process_manual_reply(pid, r["body"])
                                        pending_email.pop(i)
                                        st.session_state["pending_email_replies"] = pending_email
                                        st.success("Behandlat — ligger nu i svarskön nedan.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Fel: {e}")
                        else:
                            st.caption("Okänd kontakt")

    # ── LinkedIn-svar (Unipile) ────────────────────────────────────────
    from integrations import linkedin_inbox as _inbox
    if _inbox.is_configured():
        if st.button("🔄 Kolla inkorgen automatiskt nu (Unipile)"):
            with st.spinner("Läser LinkedIn-svar..."):
                try:
                    r = inbox_watcher.check_inbox()
                    st.success(f"{len(r['new_replies'])} nya svar, {r['unmatched']} omatchade.")
                except Exception as e:
                    st.error(f"Fel: {e}")
    else:
        st.caption("💡 Vill du ha det helt automatiskt senare? Koppla Unipile (se README).")

    try:
        replies = db.get_inbox_replies(handled=False)
    except Exception as e:
        st.error(f"Kunde inte hämta svar: {e}")
        replies = []

    if not replies:
        st.info("Inga osvarade svar just nu. 🎉")
    else:
        st.subheader(f"{len(replies)} svar väntar på dig")
        from agents.conversation import stage_label
        for r in replies:
            p = r.get("prospects") or {}
            namn = p.get("namn", r.get("sender_name", "Okänd"))
            bolag = p.get("bolag", "")
            kat = r.get("kategori") or "?"
            steg = stage_label(r.get("steg", ""))
            with st.container(border=True):
                st.markdown(f"### {namn} @ {bolag}")
                st.caption(f"Kategori: {kat} · Säljtrappa: {steg}")
                st.markdown("**De skrev:**")
                st.info(r.get("text", ""))
                st.markdown("**Ditt nästa meddelande** (redigera, kopiera, skicka):")
                edited = st.text_area("Redigera innan du skickar",
                                      value=r.get("suggested_reply", ""),
                                      key=f"reply_{r['id']}", height=110)
                st.code(edited, language=None)
                b1, b2 = st.columns([2, 1])
                with b1:
                    if st.button("✅ Klar / hanterad", key=f"done_{r['id']}", type="primary",
                                 use_container_width=True):
                        try:
                            db.mark_reply_handled(r["id"])
                            st.success("Markerad som hanterad!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")
                with b2:
                    if st.button("❌ Avböj", key=f"reject_reply_{r['id']}",
                                 use_container_width=True,
                                 help="Kontakten är inte intresserad — stäng och arkivera."):
                        try:
                            db.mark_reply_handled(r["id"])
                            pid = (r.get("prospects") or {}).get("id") or r.get("prospect_id")
                            if pid:
                                db.update_prospect_status(pid, "avbojd")
                            st.success("Avböjd och stängd.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")


def _render_followups_tab():
    try:
        due = get_followups_due()
    except Exception as e:
        st.error(f"Fel: {e}")
        due = []

    closes = [d for d in due if d["action"] == "close"]
    followups = [d for d in due if d["action"] != "close"]

    if not due:
        st.success("✅ Inga uppföljningar att göra idag!")

    if followups:
        st.subheader(f"📬 {len(followups)} uppföljningar att skicka")
        for item in followups:
            p = item["prospect"]
            is_f2 = item["action"] == "followup_2"
            action_label = "Uppföljning 2 — dag 7" if is_f2 else "Uppföljning 1 — dag 3"
            with st.container(border=True):
                st.markdown(f"### {p['namn']} @ {p.get('bolag','')}")
                st.caption(action_label + " · "
                           + person_link_inline(p.get("namn", ""), p.get("bolag", ""),
                                                p.get("linkedin_url", "")))

                # Dag 7: visa telefon + ringpåminnelse om det finns
                if is_f2:
                    telefon = p.get("telefon", "")
                    if telefon:
                        st.info(f"📞 **Ring upp nu?** {p.get('namn','').split()[0]} "
                                f"på **{telefon}** — du kan ringa parallellt med "
                                f"eller istället för uppföljningsmejlet.")
                    else:
                        st.caption("📞 Inget telefonnummer registrerat — "
                                   "lägg till i Översikt om du hittar det.")

                st.code(item["message"], language=None)
                b1, b2 = st.columns([2, 1])
                with b1:
                    if st.button("✅ Mejl skickat", key=f"fsent_{p['id']}",
                                 type="primary", use_container_width=True):
                        try:
                            dm = db.insert_dm(p["id"], item["message"], typ=item["action"])
                            db.mark_dm_skickad(dm["id"])
                            db.update_prospect_status(p["id"], item["action"])
                            st.success("Markerat!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")
                with b2:
                    if st.button("❌ Avböj", key=f"freject_{p['id']}",
                                 use_container_width=True,
                                 help="Inte intresserad — ta bort ur uppföljningskön."):
                        try:
                            db.update_prospect_status(p["id"], "avbojd")
                            st.success("Avböjd.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")

    if closes:
        st.divider()
        st.subheader(f"🔒 {len(closes)} att stänga (inget svar)")
        for item in closes:
            p = item["prospect"]
            col1, col2 = st.columns([3, 1])
            col1.write(f"{p['namn']} — {p.get('bolag','')}")
            with col2:
                if st.button("Stäng", key=f"close_{p['id']}"):
                    try:
                        process_close(p["id"])
                        st.success("Stängd.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")
