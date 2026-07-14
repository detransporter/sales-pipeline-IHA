"""🌱 Leads — hitta person + godkänn (en enda lista)."""

import re

import streamlit as st

from agents import people_finder
from integrations import apify_research as _apify
from integrations import email_sender
from agents import company_analyzer
from database import supabase_client as db
from views.shared import (goto, person_link_inline, render_company_analysis,
                          render_email_composer, log_sent_email, kategori_label)

# Open Brain-minnet (tålig import — kortet funkar även utan det).
try:
    from brain import open_brain as _brain
except Exception:
    _brain = None

# Hur många leads auto-körningen bearbetar per omgång (personsök är långsamt/kostar,
# så vi tar några i taget och ritar om — trilar igenom listan utan att frysa sidan).
AUTO_BATCH = 3


def _enrich_lead(l, contact_cache) -> dict:
    """
    Berika ETT lead: hemsida (gratis gissning) + e-post + telefon (gratis) och rätt
    person (find_person — gratis web search först, Apify bara om krediter). Tålig:
    fel på ett steg stoppar inte de andra. Returnerar vad som hittades.
    """
    lid = l["id"]
    had_web = bool((l.get("website") or "").strip())
    web = (l.get("website") or "").strip()
    res = {"web": False, "mail": False, "tel": False, "person": False}

    if not web:
        try:
            web = _apify.guess_company_website(l.get("bolag", "")) or ""
        except Exception:
            web = ""
    if web:
        try:
            contact = _apify.find_emails(web, l.get("bolag", ""), render=False)
            email = contact.get("best", "") or contact.get("guessed", "")
            tel = contact.get("telefon", "")
            contact_cache[lid] = {**contact, "website": web}
            db.update_lead_suggestion_contact(lid, email=email, website=web, telefon=tel)
            res["web"] = not had_web
            res["mail"] = bool(contact.get("best"))
            res["tel"] = bool(tel)
        except Exception:
            pass
    if not l.get("namn"):
        try:
            found = people_finder.find_person(
                l.get("bolag", ""), web, l.get("titel", ""), l.get("bransch", ""))
            if found.get("namn"):
                db.update_lead_suggestion_person(
                    lid, found["namn"], found.get("titel", ""),
                    found.get("linkedin_url", ""))
                res["person"] = True
        except Exception:
            pass
    return res


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
        # ── EN knapp: bearbeta hela den godkända listan (gratis-först) ──────
        # Ett svep hittar hemsida + e-post + telefon (gratis) och rätt person
        # (gratis web search först, Apify bara om krediter finns).
        need_work = [l for l in pending
                     if l.get("id") and (not (l.get("website") or "").strip()
                                         or not l.get("namn"))]
        if need_work:
            bcol, tcol = st.columns([2, 1])
            with bcol:
                run_now = st.button(f"🚀 Bearbeta {len(need_work)} nya leads",
                                    type="primary", key="bulk_enrich",
                                    use_container_width=True)
            with tcol:
                auto = st.toggle(
                    "⚡ Auto", key="auto_enrich",
                    help="Bearbetar nya leads automatiskt, några i taget, tills listan är "
                         "klar. Startar av sig själv när du sparar nya leads. Drar krediter "
                         "för personsök — stäng av när du vill.")
            st.caption("Hittar hemsida, e-post, telefon och rätt person. Hemsida/e-post/"
                       "telefon är gratis; personsök körs gratis (web search) och faller "
                       "bara tillbaka på Apify om det finns krediter.")

            # ── Manuell körning: hela listan i ett svep med progressbar ──
            if run_now:
                prog = st.progress(0.0, text="Bearbetar leads...")
                tot = {"web": 0, "mail": 0, "tel": 0, "person": 0}
                for i, l in enumerate(need_work):
                    r = _enrich_lead(l, contact_cache)
                    for k in tot:
                        tot[k] += int(r[k])
                    prog.progress((i + 1) / len(need_work),
                                  text=f"Bearbetat {i + 1}/{len(need_work)} bolag")
                st.session_state["apify_credit"] = _apify.remaining_usage_usd()
                st.success(f"Klart — av {len(need_work)}: hemsida +{tot['web']}, e-post "
                           f"+{tot['mail']}, telefon +{tot['tel']}, person +{tot['person']}.")
                st.rerun()

            # ── Auto-körning: trilar igenom listan i satser, self-terminating ──
            # Varje lead försöks EN gång per session (auto_done) så leads som inte
            # går att hitta inte loopar för evigt.
            done_ids = st.session_state.setdefault("auto_done", set())
            todo = [l for l in need_work if l["id"] not in done_ids]
            if auto and todo:
                batch = todo[:AUTO_BATCH]
                done_before = len(need_work) - len(todo)
                with st.spinner(f"⚡ Auto-bearbetar… {done_before + len(batch)}/{len(need_work)}"):
                    for l in batch:
                        _enrich_lead(l, contact_cache)
                        done_ids.add(l["id"])
                st.session_state["apify_credit"] = _apify.remaining_usage_usd()
                st.rerun()          # fortsätt med nästa sats
            elif auto:
                st.caption("✅ Auto-bearbetning klar för den här omgången.")
        else:
            st.caption("✅ Alla leads har hemsida och person — godkänn nedan för pipeline.")

        st.divider()

        # ── Sortering + filter (hjälper när listan är lång) ──────────────────
        # Valet sparas i beständiga session-fält (…_v) så det minns sig även efter
        # sidbyte — Streamlit rensar annars widget-state för sidor som inte visas.
        SORT_OPTS = ["IHA-score (högst)", "Lagerandel (högst)", "Bolag (A–Ö)", "Nyast först"]
        FILT_OPTS = ["Alla", "Saknar person", "Har person (redo att godkänna)",
                     "Saknar hemsida/e-post"]
        _s = st.session_state.get("leads_sort_v", SORT_OPTS[0])
        _f = st.session_state.get("leads_filter_v", FILT_OPTS[0])
        scol, fcol = st.columns(2)
        with scol:
            sort_by = st.selectbox("Sortera", SORT_OPTS,
                                   index=SORT_OPTS.index(_s) if _s in SORT_OPTS else 0,
                                   key="leads_sort")
        with fcol:
            filt = st.selectbox("Visa", FILT_OPTS,
                                index=FILT_OPTS.index(_f) if _f in FILT_OPTS else 0,
                                key="leads_filter")
        st.session_state["leads_sort_v"] = sort_by
        st.session_state["leads_filter_v"] = filt

        def _num(v):
            try:
                return float(v)
            except Exception:
                return -1.0

        view = list(pending)
        if filt == "Saknar person":
            view = [l for l in view if not (l.get("namn") or "").strip()]
        elif filt == "Har person (redo att godkänna)":
            view = [l for l in view if (l.get("namn") or "").strip()]
        elif filt == "Saknar hemsida/e-post":
            view = [l for l in view if not (l.get("website") or "").strip()
                    or not (l.get("email") or "").strip()]

        if sort_by == "IHA-score (högst)":
            view.sort(key=lambda l: _num(l.get("score")), reverse=True)
        elif sort_by == "Lagerandel (högst)":
            view.sort(key=lambda l: _num(l.get("lagerandel")), reverse=True)
        elif sort_by == "Bolag (A–Ö)":
            view.sort(key=lambda l: (l.get("bolag") or "").lower())
        elif sort_by == "Nyast först":
            view.sort(key=lambda l: (l.get("created_at") or ""), reverse=True)

        st.caption(f"Visar {len(view)} av {len(pending)} leads "
                   f"— sorterat på {sort_by.lower()}.")
        for l in view:
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
            _kb = kategori_label(l.get("kategori"))
            st.markdown(f"{(_kb + ' · ') if _kb else ''}"
                        f"**{l.get('bolag')}** — {l.get('titel')} · "
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

        # ── Snabb-genväg: klistra in kontakt (för hårda bolag skraparen missar) ──
        with st.expander("📋 Klistra in kontakt"):
            raw = st.text_area(
                "Klistra in namn / e-post / telefon (valfritt format)",
                key=f"paste_{lid}", height=90,
                placeholder="Tony Ekström, VD\ntony@soliferpolar.com\n0942-520 00")
            if st.button("📋 Tolka & spara", key=f"paste_save_{lid}", type="primary"):
                txt = raw or ""
                _mails = _apify._EMAIL_RE.findall(txt)
                _phones = _apify._extract_phones(txt)
                p_email = _mails[0].strip().lower() if _mails else ""
                p_tel = _phones[0] if _phones else ""
                # Namn: bara om leaden saknar person. Ta första rad utan @ och utan
                # långt sifferblock (2–5 ord), som ser ut som ett namn.
                p_namn = ""
                if not (l.get("namn") or "").strip():
                    for line in txt.splitlines():
                        # Dra bort ev. roll efter komma: "Tony Ekström, VD" → "Tony Ekström"
                        cand = line.strip().split(",")[0].strip()
                        if (cand and "@" not in cand and not re.search(r"\d{4,}", cand)
                                and 2 <= len(cand.split()) <= 4):
                            p_namn = cand
                            break
                if not (p_email or p_tel or p_namn):
                    st.warning("Hittade varken namn, e-post eller telefon i texten.")
                else:
                    try:
                        _old_n = (l.get("namn") or "").strip()
                        if p_namn and _brain and _brain.is_configured():
                            try:
                                _brain.capture_thought(
                                    (f"[people_finder-lärdom] {l.get('bolag','')} "
                                     f"({l.get('bransch','')}): agenten hade ingen person, "
                                     f"David klistrade in \"{p_namn}\" — leta djupare på "
                                     "Om oss/Ledning/Kontakt för liknande bolag.")[:400])
                            except Exception:
                                pass
                        if p_namn:
                            db.update_lead_suggestion_person(
                                lid, p_namn, l.get("titel", ""), l.get("linkedin_url", ""))
                        if p_email or p_tel or website:
                            db.update_lead_suggestion_contact(
                                lid, email=p_email, website=website, telefon=p_tel)
                        st.success(f"Sparat — namn: {p_namn or '(oförändrat)'} · "
                                   f"e-post: {p_email or '—'} · telefon: {p_tel or '—'}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fel: {e}")

        # Sekundära åtgärder samlade under EN panel (tre flikar) så listan blir
        # lätt att skanna. Öppna bara det kort du jobbar med.
        with st.expander("➕ Mer — kontakt, IHA-analys & mejl"):
            tab_kontakt, tab_analys, tab_mejl = st.tabs(
                ["✏️ Kontakt", "📊 IHA-analys", "📧 Mejl"])

            # ── Flik: manuell kontakt (när automatik inte hittar rätt person) ──
            with tab_kontakt:
                with st.form(key=f"manual_{lid}"):
                    # OBS: .get(key, "") ger None om kolumnen finns men är null →
                    # text_input returnerar då None och .strip() kraschar. Tvinga str.
                    m_namn  = st.text_input("Namn", value=l.get("namn") or "",
                                            placeholder="Anna Lindqvist")
                    m_titel = st.text_input("Roll", value=l.get("titel") or "",
                                            placeholder="Inköpschef")
                    m_li    = st.text_input("LinkedIn-URL (valfritt)",
                                            value=l.get("linkedin_url") or "",
                                            placeholder="https://linkedin.com/in/...")
                    c1, c2 = st.columns(2)
                    with c1:
                        m_email = st.text_input("E-post (valfritt)",
                                                value=l.get("email") or "",
                                                placeholder="anna.lindqvist@foretag.se")
                    with c2:
                        m_tel = st.text_input("Telefon (valfritt)",
                                              value=l.get("telefon") or "",
                                              placeholder="+46 70 123 45 67")
                    if st.form_submit_button("💾 Spara"):
                        try:
                            # Lär agenten via Open Brain. TVÅ fall:
                            #  1. Rättning: David skriver över en felgissning.
                            #  2. Lärdom: agenten hittade INGEN, David hittade personen
                            #     själv (t.ex. via hemsidan) — den mest värdefulla signalen.
                            # Sparas → återanvänds av find_person för liknande bolag.
                            _old = (l.get("namn") or "").strip()
                            _new = (m_namn or "").strip()
                            _rol = (m_titel or "").strip()
                            if _new and _new != _old and _brain and _brain.is_configured():
                                if _old:
                                    _note = (f"[people_finder-rättning] {l.get('bolag','')} "
                                             f"({l.get('bransch','')}): agenten gissade "
                                             f"\"{_old}\" men rätt person är \"{_new}\""
                                             + (f", {_rol}" if _rol else "")
                                             + ". Vikta den rollen/källan högre för liknande bolag.")
                                else:
                                    _note = (f"[people_finder-lärdom] {l.get('bolag','')} "
                                             f"({l.get('bransch','')}): agenten hittade ingen "
                                             f"person, men rätt kontakt är \"{_new}\""
                                             + (f", {_rol}" if _rol else "")
                                             + " — hittad manuellt på hemsidan. Leta djupare på "
                                             "Om oss/Ledning/Kontakt-sidor för liknande bolag.")
                                try:
                                    _brain.capture_thought(_note[:400])
                                except Exception:
                                    pass
                            _li = (m_li or "").strip()
                            _email = (m_email or "").strip()
                            _tel = (m_tel or "").strip()
                            if _new:
                                db.update_lead_suggestion_person(
                                    lid, _new, _rol, _li)
                            if _email or _tel or website:
                                db.update_lead_suggestion_contact(
                                    lid, email=_email,
                                    website=website, telefon=_tel)
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
