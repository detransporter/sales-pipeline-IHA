"""🌱 Leads — hitta person + godkänn (en enda lista)."""

import streamlit as st

from agents import people_finder
from integrations import apify_research as _apify
from integrations import email_sender
from agents import company_analyzer
from database import supabase_client as db
from views.shared import (goto, person_link_inline, render_company_analysis,
                          render_email_composer, log_sent_email)


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
        missing = [l for l in pending if l.get("id") and not l.get("namn")]
        if missing:
            st.caption(f"💡 {len(missing)} lead(s) saknar person. Bulk-sök hittar person, "
                       f"hemsida OCH e-post per bolag (drar Apify-krediter).")
            if st.button(f"🔍 Hitta person + e-post för alla ({len(missing)})", type="primary",
                         key="bulk_people"):
                prog = st.progress(0.0)
                found_n = 0
                for i, l in enumerate(missing):
                    try:
                        found = people_finder.find_person(
                            l.get("bolag", ""), l.get("website", ""),
                            l.get("titel", ""), l.get("bransch", ""))
                        if found.get("namn"):
                            db.update_lead_suggestion_person(
                                l["id"], found["namn"],
                                found.get("titel", ""), found.get("linkedin_url", ""))
                            found_n += 1
                    except Exception:
                        pass
                    # Hemsida + e-post (backup-väg in om LinkedIn inte funkar)
                    try:
                        contact = _apify.find_emails(l.get("website", ""), l.get("bolag", ""),
                                                     render=True)
                        if contact.get("website") or contact.get("best") or contact.get("guessed"):
                            contact_cache[l["id"]] = contact
                            db.update_lead_suggestion_contact(
                                l["id"], email=contact.get("best", "") or contact.get("guessed", ""),
                                website=contact.get("website", ""))
                    except Exception:
                        pass
                    prog.progress((i + 1) / len(missing))
                st.success(f"Hittade person på {found_n} av {len(missing)} bolag. "
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
            if l.get("telefon"):
                st.markdown(f"📞 {l['telefon']}")
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
                            website=contact.get("website", ""))
                        if contact.get("best"):
                            via = " (via renderad sida)" if contact.get("rendered") else ""
                            st.success(f"Hittade {len(contact['emails'])} adress(er){via}.")
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
                             omsattning_msek=l.get("omsattning")),
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
