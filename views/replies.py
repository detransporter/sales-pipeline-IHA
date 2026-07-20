"""💬 Svar & uppföljning — inkomna svar + uppföljningar (sammanslaget)."""

from datetime import date, timedelta

import streamlit as st

from agents import inbox_watcher
from agents.followup import get_followups_due, process_close, postpone_followup
from database import supabase_client as db
from views.shared import (person_link_inline, render_email_composer, log_sent_email,
                          kategori_label)


def _reply_subject(prospect_id: str, bolag: str) -> str:
    """Ämnesrad för svar: 'Re: <senaste mejlämnet>' så tråden hålls ihop i
    mottagarens inkorg. Fallback: bolagsnamnet."""
    try:
        for m in db.get_sent_emails(limit=200):
            if m.get("prospect_id") == prospect_id:
                for line in (m.get("meddelande") or "").splitlines():
                    if line.lower().startswith("ämne:"):
                        subj = line.split(":", 1)[1].strip()
                        if subj:
                            return subj if subj.lower().startswith("re:") else f"Re: {subj}"
                break
    except Exception:
        pass
    return f"Re: {bolag}" if bolag else "Re: vårt mejl"


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
                            saved = inbox_watcher.process_manual_reply(opt[chosen], reply_text)
                            if saved.get("kategori") == "AUTOSVAR":
                                st.info(f"🏖 Autosvar upptäckt — uppföljningen är pausad "
                                        f"till **{saved.get('_atergang', '?')}**. "
                                        f"Inget mer att göra.")
                            else:
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
                                        saved = inbox_watcher.process_manual_reply(pid, r["body"])
                                        pending_email.pop(i)
                                        st.session_state["pending_email_replies"] = pending_email
                                        if saved.get("kategori") == "AUTOSVAR":
                                            st.info(f"🏖 Autosvar — uppföljning pausad till "
                                                    f"{saved.get('_atergang', '?')}.")
                                        else:
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
            pid = (r.get("prospects") or {}).get("id") or r.get("prospect_id")
            email = (p.get("email") or "").strip()
            _kb = kategori_label(p.get("kategori"))
            with st.container(border=True):
                st.markdown(f"### {(_kb + ' · ') if _kb else ''}{namn} @ {bolag}")
                st.caption(f"Svarstyp: {kat} · Säljtrappa: {steg}")
                st.markdown("**De skrev:**")
                st.info(r.get("text", ""))
                st.markdown("**Ditt nästa meddelande** (redigera och skicka):")
                edited = st.text_area("Redigera innan du skickar",
                                      value=r.get("suggested_reply", ""),
                                      key=f"reply_{r['id']}", height=110)

                # Skicka direkt via mejl om kontakten har e-post — annars kopiera.
                if email:
                    b0, b1, b2 = st.columns([2, 2, 1])
                    with b0:
                        if st.button(f"📨 Skicka via mejl", key=f"send_{r['id']}",
                                     type="primary", use_container_width=True,
                                     help=f"Skickar texten ovan till {email} och "
                                          f"markerar svaret som hanterat."):
                            from integrations import email_sender
                            subject = _reply_subject(pid, bolag)
                            ok, err = email_sender.send_email(email, subject, edited)
                            if ok:
                                try:
                                    log_sent_email(pid, email, subject, edited)
                                    db.mark_reply_handled(r["id"])
                                except Exception:
                                    pass
                                st.success(f"✅ Skickat till {email} — hanterat.")
                                st.rerun()
                            else:
                                st.error(err)
                else:
                    st.code(edited, language=None)
                    st.caption("Ingen e-post på kontakten — kopiera texten och "
                               "skicka via LinkedIn, markera sedan som hanterad.")
                    b1, b2 = st.columns([2, 1])

                with b1:
                    if st.button("✅ Klar / hanterad", key=f"done_{r['id']}",
                                 type="primary" if not email else "secondary",
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
        st.subheader(f"📬 {len(followups)} kontakter att följa upp")
        st.caption("Mejla igen, ring med manus, eller markera hur du följt upp. "
                   "Varje åtgärd loggas och flyttar kontakten framåt.")
        for item in followups:
            _render_followup_card(item)

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


def _render_followup_card(item):
    """En uppföljning: mejla igen / ringa med manus / markera — alltid bekräftat."""
    from integrations import email_sender
    from agents import email_writer

    p = item["prospect"]
    pid = p["id"]
    step = 2 if item["action"] == "followup_2" else 1
    label = f"Uppföljning {step} — dag {'7' if step == 2 else '3'}"
    telefon = (p.get("telefon") or "").strip()
    email = (p.get("email") or "").strip()
    fornamn = (p.get("namn") or "").split()[0] if p.get("namn") else "kontakten"

    def _advance(logmsg, typ, new_status, meeting_date=None):
        """Logga åtgärden + flytta kontakten framåt (+ ev. boka möte), uppdatera vyn."""
        try:
            dm = db.insert_dm(pid, logmsg, typ=typ)
            db.mark_dm_skickad(dm["id"])
            db.update_prospect_status(
                pid, new_status,
                meeting_date=meeting_date.isoformat() if meeting_date else None)
            if meeting_date is not None:
                db.insert_meeting(pid, meeting_date.isoformat())
                st.success(f"✅ Möte bokat {meeting_date} — syns nu under 📅 Möten.")
            else:
                st.success("✅ Klart — loggat och uppdaterat.")
            st.rerun()
        except Exception as e:
            st.error(f"Fel: {e}")

    with st.container(border=True):
        head, act = st.columns([5, 1])
        with head:
            _namn = (p.get("namn") or "").strip()
            _titel = f"{_namn} @ {p.get('bolag','')}" if _namn else p.get("bolag", "(okänt bolag)")
            _kb = kategori_label(p.get("kategori"))
            st.markdown(f"### {(_kb + ' · ') if _kb else ''}{_titel}")
            st.caption(f"{label} · Kontaktad, inget svar ännu · "
                       + person_link_inline(p.get("namn", ""), p.get("bolag", ""),
                                            p.get("linkedin_url", "")))
        with act:
            # Alltid synlig, men med bekräftelse så inget avvisas av misstag.
            confirm_key = f"fu_reject_confirm_{pid}"
            if not st.session_state.get(confirm_key):
                if st.button("🚫 Avvisa", key=f"fu_reject_{pid}",
                             use_container_width=True,
                             help="Markera som inte intresserad och ta bort ur "
                                  "uppföljningskön."):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.caption("Säker?")
                if st.button("✅ Ja, avvisa", key=f"fu_reject_yes_{pid}",
                             type="primary", use_container_width=True):
                    try:
                        db.update_prospect_status(pid, "avbojd")
                        st.session_state.pop(confirm_key, None)
                        st.success(f"Avvisade {p.get('bolag','kunden')}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")
                if st.button("Avbryt", key=f"fu_reject_no_{pid}",
                             use_container_width=True):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()

        # ── Skjut upp: mottagaren ledig? Flytta fram nästa kontakt ──────────
        with st.expander("📅 Skjut upp (t.ex. semester)"):
            st.caption("Flytta fram nästa kontaktdatum. Kontakten försvinner ur kön "
                       "och dyker upp igen den dag du väljer.")
            pc1, pc2, pc3 = st.columns(3)
            quick = None
            if pc1.button("+1 vecka", key=f"pp1_{pid}", use_container_width=True):
                quick = date.today() + timedelta(days=7)
            if pc2.button("+2 veckor", key=f"pp2_{pid}", use_container_width=True):
                quick = date.today() + timedelta(days=14)
            if pc3.button("Efter 15 aug", key=f"pp3_{pid}", use_container_width=True):
                quick = max(date.today() + timedelta(days=1), date(date.today().year, 8, 15))
            valt = st.date_input("…eller välj datum", value=date.today() + timedelta(days=14),
                                 min_value=date.today() + timedelta(days=1),
                                 key=f"pp_date_{pid}")
            do_it = st.button("📅 Skjut upp till valt datum", key=f"pp_go_{pid}")
            target = quick or (valt if do_it else None)
            if target:
                try:
                    postpone_followup(pid, item["action"], target)
                    st.success(f"✅ Uppskjuten — dyker upp igen {target.isoformat()}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

        tab_mail, tab_call, tab_other = st.tabs(
            ["📧 Mejla uppföljning", "📞 Ring", "✔️ Markera manuellt"])

        # ── Mejla igen (företagsunikt uppföljningsmejl) ──
        with tab_mail:
            if email:
                to, subj, body, send = render_email_composer(
                    f"fu_{pid}", email,
                    dict(bolag=p.get("bolag", ""), namn=p.get("namn", ""),
                         titel=p.get("titel", ""), bransch=p.get("bransch", ""),
                         lagerandel=p.get("lagerandel"), varulager_msek=p.get("varulager"),
                         omsattning_msek=p.get("omsattning"), orgnr=p.get("orgnr", ""),
                         website=p.get("website", ""), followup_steg=step),
                    to_options=[email])
                if send:
                    ok, err = email_sender.send_email(to, subj, body)
                    if ok:
                        log_sent_email(pid, to, subj, body)
                        db.update_prospect_status(pid, item["action"])
                        st.success(f"✅ Uppföljningsmejl skickat till {to}.")
                        st.rerun()
                    else:
                        st.error(err)
            else:
                st.caption("Ingen e-post sparad på kontakten — lägg till den i 📊 Översikt, "
                           "eller följ upp via Ring/LinkedIn.")

        # ── Ring, med företagsunikt manus + bekräftat utfall ──
        with tab_call:
            if telefon:
                st.markdown(f"## 📞 [{telefon}](tel:{telefon})")
                st.caption(f"Ring {fornamn}. Tryck **Skriv ringmanus** för ett kort, "
                           "företagsunikt manus att läsa rakt av.")
                skey = f"call_script_{pid}"
                if st.button("📝 Skriv ringmanus", key=f"gen_call_{pid}"):
                    with st.spinner("Skriver företagsunikt ringmanus..."):
                        st.session_state[skey] = email_writer.generate_call_script(
                            bolag=p.get("bolag", ""), namn=p.get("namn", ""),
                            titel=p.get("titel", ""), bransch=p.get("bransch", ""),
                            orgnr=p.get("orgnr", ""), website=p.get("website", ""),
                            lagerandel=p.get("lagerandel"), varulager_msek=p.get("varulager"),
                            omsattning_msek=p.get("omsattning"))
                if st.session_state.get(skey):
                    st.text_area("Ringmanus", value=st.session_state[skey], height=230,
                                 key=f"call_area_{pid}")
                st.divider()
                utfall = st.radio(
                    "Hur gick samtalet?",
                    ["Bokade möte", "Intresserad – följ upp senare",
                     "Inget svar / röstbrevlåda", "Inte intresserad"],
                    key=f"call_out_{pid}")
                note = st.text_input("Anteckning (valfritt)", key=f"call_note_{pid}",
                                     placeholder="t.ex. ringer tillbaka nästa vecka")
                mote_datum = None
                if utfall == "Bokade möte":
                    mote_datum = st.date_input(
                        "Mötesdatum", value=date.today() + timedelta(days=3),
                        key=f"call_date_{pid}",
                        help="Skapar en mötespost automatiskt under 📅 Möten.")
                if st.button("✅ Bekräfta samtal", key=f"call_confirm_{pid}", type="primary"):
                    logmsg = f"📞 Ringde {telefon}. Utfall: {utfall}." + (f" {note}" if note else "")
                    new_status = ("mote_bokat" if utfall == "Bokade möte"
                                  else "avbojd" if utfall == "Inte intresserad"
                                  else item["action"])
                    _advance(logmsg, "call", new_status, meeting_date=mote_datum)
            else:
                st.caption("📞 Inget telefonnummer sparat på kontakten.")
                newtel = st.text_input("Lägg till telefonnummer", key=f"add_tel_{pid}",
                                       placeholder="+46 70 123 45 67")
                if st.button("💾 Spara nummer", key=f"save_tel_{pid}"):
                    if newtel.strip():
                        try:
                            db.update_prospect(pid, {"telefon": newtel.strip()})
                            st.success("Sparat — öppna fliken igen för att ringa.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")

        # ── Markera manuellt (följt upp utanför appen) / avböj ──
        with tab_other:
            st.caption("Följde du upp på annat sätt (LinkedIn, mejl i din klient)? "
                       "Kopiera vid behov och bekräfta här:")
            st.code(item["message"], language=None)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Jag har följt upp", key=f"fu_done_{pid}",
                             type="primary", use_container_width=True):
                    _advance(item["message"], item["action"], item["action"])
            with c2:
                if st.button("❌ Inte intresserad", key=f"freject_{pid}",
                             use_container_width=True,
                             help="Ta bort ur uppföljningskön."):
                    try:
                        db.update_prospect_status(pid, "avbojd")
                        st.success("Avböjd.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")
