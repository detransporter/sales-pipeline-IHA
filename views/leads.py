"""🌱 Leads — hitta person + godkänn (en enda lista)."""

import streamlit as st

from agents import people_finder
from integrations import apify_research as _apify
from integrations import email_sender
from agents import company_analyzer
from database import supabase_client as db
from views.shared import (goto, person_link_inline, render_company_analysis,
                          render_email_composer, log_sent_email)

# Open Brain-minnet (tålig import — kortet funkar även utan det).
try:
    from brain import open_brain as _brain
except Exception:
    _brain = None


def render():
    st.title("🌱 Leads")
    st.caption("Sparade leads som väntar på person + godkännande. "
               "Hitta person → verifiera på LinkedIn → godkänn för att lägga i pipeline.")

    try:
        pending = db.get_lead_suggestions(status="pending")
    except Exception as e:
        pending = []
        st.error(f"Kunde inte läsa leads: {e}")

    # Hittade hemsidor/e-post under sessionen (visas även om DB saknar email-kolumn)
    contact_cache = st.session_state.setdefault("lead_contact", {})
    # Bolagsanalyser (genereras på knapptryck, cachas så vi inte drar API per omritning)
    analysis_cache = st.session_state.setdefault("lead_analysis", {})
    # Mejlstatus: bolagsnamn (lower) → datum för senast skickat mejl
    try:
        _sent = db.get_sent_emails(limit=200)
        _emailed_bolag: dict[str, str] = {
            (m.get("prospects") or {}).get("bolag", "").lower(): (m.get("skickad_at") or "")[:10]
            for m in _sent
            if (m.get("prospects") or {}).get("bolag")
        }
    except Exception:
        _emailed_bolag = {}

    if not pending:
        st.info("Inga leads väntar. Hitta nya bolag under 🔍 Hitta bolag.")
        st.button("🔍 Gå till Hitta bolag →", on_click=goto, args=("🔍 Hitta bolag",))
    else:
        no_web = [l for l in pending if l.get("id") and not (l.get("website") or "").strip()]
        no_person = [l for l in pending if l.get("id") and not l.get("namn")]

        # ── GRATIS: hitta hemsidor + e-post (ingen Apify) ───────────────────
        if no_web:
            st.caption(f"🌐 {len(no_web)} lead(s) saknar hemsida. Gissar domänen ur "
                       f"bolagsnamnet och skrapar publik e-post — **helt gratis**, "
                       f"inga Apify-krediter.")
            if st.button(f"🌐 Hitta hemsidor + e-post gratis ({len(no_web)})",
                         type="primary", key="bulk_web"):
                prog = st.progress(0.0)
                web_n = mail_n = tel_n = 0
                for i, l in enumerate(no_web):
                    try:
                        web = _apify.guess_company_website(l.get("bolag", ""))
                        if web:
                            contact = _apify.find_emails(web, l.get("bolag", ""), render=False)
                            email = contact.get("best", "") or contact.get("guessed", "")
                            tel = contact.get("telefon", "")
                            contact_cache[l["id"]] = {**contact, "website": web}
                            db.update_lead_suggestion_contact(l["id"], email=email,
                                                              website=web, telefon=tel)
                            web_n += 1
                            if contact.get("best"):
                                mail_n += 1
                            if tel:
                                tel_n += 1
                    except Exception:
                        pass
                    prog.progress((i + 1) / len(no_web))
                st.success(f"Hittade hemsida för {web_n} av {len(no_web)} bolag "
                           f"({mail_n} med e-post, {tel_n} med telefon) — gratis. "
                           f"Verifiera innan du mejlar/ringer.")
                st.rerun()

        # ── BETALT: hitta personer via LinkedIn (drar Apify-krediter) ───────
        if no_person:
            st.caption(f"🔗 {len(no_person)} lead(s) saknar person. LinkedIn-personsök "
                       f"kräver Apify-krediter (Google-aktorn).")
            # Proaktiv kreditvarning — kollas en gång per session (ingen polling).
            if "apify_credit" not in st.session_state:
                st.session_state["apify_credit"] = _apify.remaining_usage_usd()
            _cred = st.session_state["apify_credit"]
            if _cred is not None and _cred < 0.50:
                st.warning(
                    f"⚠️ Apify-krediterna är nästan slut (~${_cred} kvar av $5/mån) — "
                    "personsök misslyckas tills du fyller på (console.apify.com/billing) "
                    "eller cykeln återställs. Gratis-knappen ovan fungerar ändå.")
            if st.button(f"🔗 Hitta personer via LinkedIn ({len(no_person)})",
                         key="bulk_people"):
                prog = st.progress(0.0)
                found_n = 0
                for i, l in enumerate(no_person):
                    # Återanvänd känd hemsida, annars gratis gissning (ingen extra kredit).
                    web = ((l.get("website") or "").strip()
                           or _apify.guess_company_website(l.get("bolag", "")))
                    try:
                        found = people_finder.find_person(
                            l.get("bolag", ""), web,
                            l.get("titel", ""), l.get("bransch", ""))
                        if found.get("namn"):
                            db.update_lead_suggestion_person(
                                l["id"], found["namn"],
                                found.get("titel", ""), found.get("linkedin_url", ""))
                            found_n += 1
                            if web and not (l.get("website") or "").strip():
                                db.update_lead_suggestion_contact(l["id"], website=web)
                    except Exception:
                        pass
                    prog.progress((i + 1) / len(no_person))
                # Uppdatera kreditsaldot och surfa upp ev. Apify-fel (t.ex. slut på krediter)
                st.session_state["apify_credit"] = _apify.remaining_usage_usd()
                if _apify.LAST_APIFY_ERROR:
                    st.error(f"⚠️ {_apify.LAST_APIFY_ERROR}")
                st.success(f"Hittade person på {found_n} av {len(no_person)} bolag. "
                           "Verifiera länkarna innan du godkänner.")
                st.rerun()

        st.divider()
        for l in pending:
            _render_lead_card(l, contact_cache, analysis_cache, _emailed_bolag)

    # Sekundärt: föreslå fler leads automatiskt (AI/Apify)
    with st.expander("➕ Hitta fler leads automatiskt (AI / Google Maps)"):
        st.caption("Komplement till bolagssöket. Föreslår bolag ur ICP och sparar som leads "
                   "(utan person — du hittar personen sen).")
        focus = st.text_input("Fokus (valfritt)",
                              placeholder="t.ex. livsmedelstillverkare i Mälardalen")
        n_new = st.number_input("Antal", 1, 15, 5)
        if st.button("Föreslå leads"):
            with st.spinner("Söker bolag..."):
                try:
                    from agents.lead_finder import suggest_leads
                    existing = db.get_existing_companies()
                    suggestions = suggest_leads(n=int(n_new), existing_companies=existing,
                                                focus=focus.strip())
                    if suggestions:
                        db.insert_lead_suggestions(suggestions)
                        st.success(f"Sparade {len(suggestions)} nya leads.")
                        st.rerun()
                    else:
                        st.info("Inga nya förslag.")
                except Exception as e:
                    st.error(f"Fel: {e}")


def _render_lead_card(l, contact_cache, analysis_cache, _emailed_bolag):
    """Ett lead-kort: person/e-post/godkänn + manuell kontakt, analys och mejl."""
    lid = l.get("id")
    cached = contact_cache.get(lid, {})
    website = cached.get("website") or l.get("website") or ""
    emails = cached.get("emails") or ([l["email"]] if l.get("email") else [])
    guessed = cached.get("guessed") or ""
    telefon = cached.get("telefon") or l.get("telefon") or ""

    with st.container(border=True):
        cols = st.columns([3, 1, 1, 1, 1])
        with cols[0]:
            st.markdown(f"**{l.get('bolag')}** — {l.get('titel')} · "
                        f"_{l.get('bransch','')}_ (score {l.get('score', 0)})")
            if l.get("namn"):
                st.markdown(f"👤 **{l['namn']}** · "
                            + person_link_inline(l["namn"], l.get("bolag", ""),
                                                 l.get("linkedin_url", "")))
            else:
                st.caption("👤 _Ingen person hittad ännu — tryck 'Hitta person'._")
            if website:
                st.markdown(f"🌐 [Företagets hemsida]({website})")
            if emails:
                links = " · ".join(f"[{e}](mailto:{e})" for e in emails[:4])
                st.markdown(f"✉️ {links}")
                st.code(emails[0], language=None)
            elif guessed:
                st.markdown(f"✉️ {guessed}  ·  _kvalificerad gissning (ej verifierad)_")
                st.code(guessed, language=None)
            if telefon:
                st.markdown(f"📞 [{telefon}](tel:{telefon})")
            if l.get("motivering"):
                st.caption(l["motivering"])
            # E-postkandidater från senaste personsökning — väntar på val
            cand_key = f"found_emails_{lid}"
            if cand_key in st.session_state:
                cands = st.session_state[cand_key]
                pat = st.session_state.get(f"found_pat_{lid}", "")
                pat_text = f" (mönster: **{pat}**)" if pat else ""
                st.info(f"📧 Välj e-postadress att spara{pat_text}:")
                sel = st.selectbox("Adress", cands,
                                   key=f"sel_email_{lid}",
                                   label_visibility="collapsed")
                if st.button("💾 Spara vald adress", key=f"save_cand_{lid}",
                             type="primary"):
                    db.update_lead_suggestion_contact(
                        lid, email=sel, website=website)
                    del st.session_state[cand_key]
                    st.session_state.pop(f"found_pat_{lid}", None)
                    st.rerun()
        with cols[1]:
            if lid and st.button("🔍 Person", key=f"person_{lid}",
                                 use_container_width=True):
                with st.spinner("Söker rätt person (hemsida + Google→LinkedIn)..."):
                    try:
                        found = people_finder.find_person(
                            l.get("bolag", ""), l.get("website", ""),
                            l.get("titel", ""), l.get("bransch", ""))
                        if found.get("namn"):
                            db.update_lead_suggestion_person(
                                l["id"], found["namn"],
                                found.get("titel", ""), found.get("linkedin_url", ""))
                            msg = f"{found['namn']} ({found.get('sakerhet','?')} säkerhet)"
                            # Spara e-postkandidater i session state → visas som selectbox i kortet
                            if found.get("email_candidates"):
                                st.session_state[f"found_emails_{lid}"] = found["email_candidates"]
                                st.session_state[f"found_pat_{lid}"] = found.get("email_pattern", "")
                                msg += " — välj e-post nedan"
                            st.success(msg)
                            st.rerun()
                        else:
                            st.warning("Hittade ingen tydlig person — kolla LinkedIn manuellt.")
                    except Exception as e:
                        st.error(f"Fel: {e}")
        with cols[2]:
            if lid and st.button("✉️ E-post", key=f"email_{lid}",
                                 use_container_width=True):
                with st.spinner("Letar e-post på hemsidan (renderar JS vid behov)..."):
                    try:
                        contact = _apify.find_emails(l.get("website", ""),
                                                     l.get("bolag", ""), render=True)
                        contact_cache[lid] = contact
                        db.update_lead_suggestion_contact(
                            lid, email=contact.get("best", "") or contact.get("guessed", ""),
                            website=contact.get("website", ""), telefon=contact.get("telefon", ""))
                        _tel = contact.get("telefon", "")
                        if contact.get("best"):
                            via = " (via renderad sida)" if contact.get("rendered") else ""
                            _telmsg = f" · 📞 {_tel}" if _tel else ""
                            st.success(f"Hittade {len(contact['emails'])} adress(er){via}{_telmsg}.")
                        elif contact.get("guessed"):
                            st.info(f"Ingen publik adress — gissar {contact['guessed']} "
                                    "(verifiera innan du mejlar).")
                        elif contact.get("website"):
                            st.warning("Hittade hemsidan men ingen publik e-post.")
                        else:
                            st.warning("Hittade ingen hemsida/e-post.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")
        with cols[3]:
            if lid and st.button("✅ Godkänn", key=f"approve_{lid}",
                                 type="primary", use_container_width=True):
                try:
                    db.promote_lead(l)
                    st.success("Tillagd i pipeline!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")
        with cols[4]:
            if lid and st.button("❌ Avböj", key=f"reject_{lid}",
                                 use_container_width=True,
                                 help="Passar inte (fel bransch/storlek e.d.) — "
                                      "tas bort ur listan."):
                try:
                    db.update_lead_suggestion(lid, "rejected")
                    st.success("Avböjd — borttagen ur leads.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

        # Mejladresser: manuellt sparad + skrapad + gissad. Behövs för mejlfliken.
        manual_email = l.get("email", "")
        all_emails = list(dict.fromkeys(
            e for e in ([manual_email] + emails + ([guessed] if guessed else []))
            if e
        ))
        _sent_date = _emailed_bolag.get((l.get("bolag") or "").lower())

        # Mejlstatus som synlig bricka på kortet — du ser den utan att öppna panelen.
        if _sent_date:
            st.success(f"✅ Mejl skickat {_sent_date}")

        # Sekundära åtgärder samlade under EN panel (tre flikar) så listan blir
        # lätt att skanna. Öppna bara det kort du jobbar med.
        with st.expander("➕ Mer — kontakt, IHA-analys & mejl"):
            tab_kontakt, tab_analys, tab_mejl = st.tabs(
                ["✏️ Kontakt", "📊 IHA-analys", "📧 Mejl"])

            # ── Flik: manuell kontakt (när automatik inte hittar rätt person) ──
            with tab_kontakt:
                with st.form(key=f"manual_{lid}"):
                    m_namn  = st.text_input("Namn", value=l.get("namn", ""),
                                            placeholder="Anna Lindqvist")
                    m_titel = st.text_input("Roll", value=l.get("titel", ""),
                                            placeholder="Inköpschef")
                    m_li    = st.text_input("LinkedIn-URL (valfritt)",
                                            value=l.get("linkedin_url", ""),
                                            placeholder="https://linkedin.com/in/...")
                    c1, c2 = st.columns(2)
                    with c1:
                        m_email = st.text_input("E-post (valfritt)",
                                                value=l.get("email", ""),
                                                placeholder="anna.lindqvist@foretag.se")
                    with c2:
                        m_tel = st.text_input("Telefon (valfritt)",
                                              value=l.get("telefon", ""),
                                              placeholder="+46 70 123 45 67")
                    if st.form_submit_button("💾 Spara"):
                        try:
                            # Rättningsfångst: skriver David över en AGENT-gissning
                            # (fanns ett namn förut som nu ändras)? Spara lärdomen i
                            # Open Brain så framtida sökningar undviker samma miss.
                            _old_namn = (l.get("namn") or "").strip()
                            if (m_namn.strip() and _old_namn
                                    and m_namn.strip() != _old_namn
                                    and _brain and _brain.is_configured()):
                                try:
                                    _brain.capture_thought(
                                        f"[people_finder-rättning] {l.get('bolag','')} "
                                        f"({l.get('bransch','')}): agenten gissade "
                                        f"\"{_old_namn}\" men rätt person är "
                                        f"\"{m_namn.strip()}\""
                                        + (f", {m_titel.strip()}" if m_titel.strip() else "")
                                        + ". Vikta den rollen/källan högre nästa gång."[:400])
                                except Exception:
                                    pass
                            if m_namn.strip():
                                db.update_lead_suggestion_person(
                                    lid, m_namn.strip(), m_titel.strip(),
                                    m_li.strip())
                            if m_email.strip() or m_tel.strip() or website:
                                db.update_lead_suggestion_contact(
                                    lid, email=m_email.strip(),
                                    website=website, telefon=m_tel.strip())
                            st.success("Sparat!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fel: {e}")

            # ── Flik: IHA-föranalys (siffror + hemsida) innan kontakt ──
            with tab_analys:
                cached_a = analysis_cache.get(lid)
                if st.button("🔬 Gör analys" if not cached_a else "🔄 Gör om analys",
                             key=f"analyze_{lid}"):
                    with st.spinner("Analyserar bolagets lagerläge (siffror + hemsida)..."):
                        try:
                            analysis_cache[lid] = company_analyzer.analyze_company(
                                bolag=l.get("bolag", ""), bransch=l.get("bransch", ""),
                                website=website, omsattning_msek=l.get("omsattning"),
                                varulager_msek=l.get("varulager"),
                                resultat_msek=l.get("resultat"),
                                anstallda=l.get("anstallda"),
                                lagerandel=l.get("lagerandel"),
                                vinstmarginal=l.get("vinstmarginal"))
                            st.rerun()
                        except Exception as e:
                            st.error(f"Kunde inte analysera: {e}")
                if cached_a:
                    render_company_analysis(cached_a)
                else:
                    st.caption("Tryck **Gör analys** — väver ihop bolagets bokslutssiffror "
                               "med deras hemsida till en säljbar bild (drar ett API-anrop).")

            # ── Flik: mejla direkt (backup-väg in om LinkedIn inte funkar) ──
            # Att mejla = att kontakta → leaden flyttas till pipeline (status
            # 'skickad') och loggas.
            with tab_mejl:
                if not all_emails:
                    st.caption("Ingen e-postadress ännu — kör 🔍 Person eller ✉️ E-post, "
                               "eller lägg in en adress under fliken Kontakt.")
                elif _sent_date:
                    st.caption(f"Redan mejlat {_sent_date}. Öppna Översikt om du vill "
                               "kontakta igen.")
                else:
                    to, subj, body, send = render_email_composer(
                        f"lead_{lid}", all_emails[0],
                        dict(bolag=l.get("bolag", ""), namn=l.get("namn", ""),
                             titel=l.get("titel", ""), bransch=l.get("bransch", ""),
                             lagerandel=l.get("lagerandel"),
                             varulager_msek=l.get("varulager"),
                             omsattning_msek=l.get("omsattning"),
                             orgnr=l.get("orgnr", ""), website=website),
                        to_options=all_emails)
                    if send:
                        ok, err = email_sender.send_email(to, subj, body)
                        if ok:
                            try:
                                prospect = db.promote_lead(l)
                                pid = prospect.get("id")
                                log_sent_email(pid, to, subj, body)
                                if pid:
                                    db.update_prospect_status(pid, "skickad")
                            except Exception:
                                pass
                            st.success(f"✅ Mejl skickat till {to} — kontakten är nu i "
                                       f"pipeline (kontaktad).")
                            st.rerun()
                        else:
                            st.error(err)
