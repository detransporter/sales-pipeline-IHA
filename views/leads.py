"""🌱 Leads — hitta person + godkänn (en enda lista).

Äger LISTAN: bulk-bearbetning (⚡ Auto/🚀 Bearbeta alla), sortering/filter,
och "hitta fler leads"-panelen. Ett enskilt kort (allt som visas/kan göras
för ETT lead) bor i `views/lead_card.py` — se den filens docstring för
varför uppdelningen gjordes.
"""

import streamlit as st

from database import supabase_client as db
from integrations import apify_research as _apify
from views.lead_card import enrich_lead, render_lead_card
from views import shared
from views.shared import goto, cached_sent_emails

# Hur många leads auto-körningen bearbetar per omgång (personsök är långsamt/kostar,
# så vi tar några i taget och ritar om — trilar igenom listan utan att frysa sidan).
AUTO_BATCH = 3


def render():
    st.title("🌱 Leads")
    st.caption("Sparade leads som väntar på person + godkännande. "
               "Hämta kontaktuppgifter → verifiera på källsidan → godkänn för att lägga i pipeline.")

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
        _sent = cached_sent_emails(limit=200)
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
        _render_bulk_enrich(pending, contact_cache)
        st.divider()
        view = _sorted_and_filtered(pending)
        for l in view:
            render_lead_card(l, contact_cache, analysis_cache, _emailed_bolag)

    # Sekundärt: föreslå fler leads automatiskt (AI/Apify)
    with st.expander("➕ Hitta fler leads automatiskt (AI / Google Maps)"):
        st.caption("Komplement till bolagssöket. Föreslår bolag ur ICP och sparar som leads "
                   "(utan person — du hittar personen sen).")
        focus = st.text_input("Fokus (valfritt)",
                              placeholder="t.ex. livsmedelstillverkare i Mälardalen")
        n_new = st.number_input("Antal", 1, 15, 5)
        if st.button("Föreslå leads"):
            with st.spinner("Söker bolag..."):
                with shared.action("Kunde inte föreslå nya leads"):
                    from agents.lead_finder import suggest_leads
                    _apify.clear_last_error()
                    existing = db.get_existing_companies()
                    suggestions = suggest_leads(n=int(n_new), existing_companies=existing,
                                                focus=focus.strip())
                    _apify_err = _apify.get_last_error()
                    if _apify_err:
                        st.warning(f"⚠️ Apify: {_apify_err} — "
                                  "föll tillbaka på AI-gissning istället för riktiga bolag.")
                    if suggestions:
                        db.insert_lead_suggestions(suggestions)
                        st.success(f"Sparade {len(suggestions)} nya leads.")
                        st.rerun()
                    else:
                        st.info("Inga nya förslag.")


def _render_bulk_enrich(pending: list, contact_cache: dict) -> None:
    """EN knapp: bearbeta hela den godkända listan (gratis-först). Ett svep
    hittar hemsida + e-post + telefon (gratis) och rätt person (gratis web
    search först, Apify bara om krediter finns)."""
    need_work = [l for l in pending
                 if l.get("id") and (not (l.get("website") or "").strip()
                                     or not l.get("namn")
                                     or not (l.get("email") or "").strip())]
    if not need_work:
        st.caption("✅ Alla leads har hemsida och person — godkänn nedan för pipeline.")
        return

    bcol, tcol = st.columns([2, 1])
    with bcol:
        run_now = st.button(f"🚀 Bearbeta {len(need_work)} nya leads",
                            type="primary", key="bulk_enrich",
                            width="stretch")
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
            r = enrich_lead(l, contact_cache)
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
                enrich_lead(l, contact_cache)
                done_ids.add(l["id"])
        st.session_state["apify_credit"] = _apify.remaining_usage_usd()
        st.rerun()          # fortsätt med nästa sats
    elif auto:
        st.caption("✅ Auto-bearbetning klar för den här omgången.")


def _sorted_and_filtered(pending: list) -> list:
    """Sortering + filter (hjälper när listan är lång). Valet sparas i
    beständiga session-fält (…_v) så det minns sig även efter sidbyte —
    Streamlit rensar annars widget-state för sidor som inte visas."""
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
    return view
