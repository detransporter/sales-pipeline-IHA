"""🔍 Hitta bolag — Allabolag-screener (letar bundet lagerkapital)."""

import re as _re

import pandas as pd
import streamlit as st

from integrations import allabolag
from agents import financial_screener as screener
from database import supabase_client as db
from views.shared import goto


def _deep_read_candidates(pool_slice):
    """Djupläs varulager per bolag (ett Allabolag-anrop styck). Returnerar fins-lista."""
    fins = []
    n = len(pool_slice)
    prog = st.progress(0.0, text="Läser varulager per bolag...")
    for i, c in enumerate(pool_slice):
        try:
            ident = c.get("_ident")
            if ident is not None:                       # egen lista (org-nr / namn)
                digits = _re.sub(r"\D", "", ident)
                fin = (allabolag.get_financials(orgnr=digits) if len(digits) == 10
                       else allabolag.get_financials(company_name=ident))
            elif c.get("orgnr"):
                fin = allabolag.get_financials(orgnr=c["orgnr"])
            else:
                fin = {}
            if fin:
                fin["website"] = fin.get("website") or c.get("website", "")
                fin["bransch"] = fin.get("bransch") or c.get("bransch", "")
                fins.append(fin)
        except Exception:
            pass
        prog.progress((i + 1) / max(1, n), text=f"Läst {i + 1}/{n} bolag")
    return fins


def _rescreen(oms_min, oms_max, max_anstallda, min_lagerandel, max_marginal, hard_margin):
    """Kör om kvalificeringen på redan djuplästa bolag — ingen ny nätverkstrafik."""
    fins = st.session_state.get("screen_fins", [])
    res = screener.screen_companies(
        fins, oms_min=oms_min, oms_max=oms_max, max_anstallda=int(max_anstallda),
        min_lagerandel=min_lagerandel, max_marginal=max_marginal, hard_margin=hard_margin)
    fn = st.session_state.get("screen_funnel", {})
    res["found"] = fn.get("found", 0)
    res["in_bransch"] = fn.get("in_bransch")
    res["bransch_val"] = fn.get("bransch_val", "")
    res["survivors"] = len(fins)
    res["pool_total"] = len(st.session_state.get("screen_pool", []))
    res["read"] = st.session_state.get("screen_read", 0)
    st.session_state["screen_result"] = res


def render():
    st.title("🔍 Hitta bolag")
    st.caption("Hittar bolag där kapital bevisligen sitter fast i lager — via Allabolags "
               "publika siffror. Filtrera på storlek, lagerandel och lönsamhet, så får du "
               "en rankad lista att spara som leads.")

    st.subheader("1. Hur ska bolagen hittas?")
    mode = st.radio(
        "Metod", ["🔎 Auto-sök (Allabolag)", "📋 Egen lista (org-nr / CSV)"],
        horizontal=True,
        help="Egen lista ger högst kvalitet: du plockar bolagen, appen läser ekonomin exakt.",
    )
    list_mode = mode.startswith("📋")

    ort, bransch_val, list_ids = "", "", []

    if not list_mode:
        st.caption("Sveper Allabolags **Segmentering** län för län (omsättning + plats, gratis), "
                   "behåller vald bransch (SNI) och läser varulager per bolag.")
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            bransch_val = st.selectbox(
                "Bransch", list(screener.BRANSCH_SNI.keys()),
                help="Lager-tunga branscher (SNI). 'Alla lager-tunga' = bredast.",
            )
        with bcol2:
            ort = st.selectbox("Län / region", ["Hela Sverige"] + screener.LAN,
                               help="Ett län, eller hela Sverige (fler bolag, längre tid).")
        fritext = st.text_input(
            "Nyckelord (valfritt)",
            placeholder="t.ex. solceller, formsprutning, livsmedel",
            help="Söker direkt på Allabolag efter bolag med det här nyckelordet. "
                 "Ersätter bransch-filtret — ger ~25 träffar men mycket specifika.",
        )
        if fritext:
            st.caption(f"🔍 Nyckelordssökning: **{fritext.strip()}** i **{ort}** "
                       f"→ ekonomifiltret tillämpas på träffarna.")
        else:
            st.caption(f"🔎 Filtrerar: **{bransch_val}** i **{ort}**")
    else:
        st.caption("Klistra in **org-nummer** (ett per rad). Eller ladda upp en CSV. "
                   "Org-nr ger exakt träff; bolagsnamn slås upp via Google (kostar lite krediter).")
        pasted = st.text_area("Org-nummer eller bolagsnamn (ett per rad)", height=130,
                              placeholder="556064-1770\n5567661631\nExempel Bolag AB")
        up = st.file_uploader("…eller ladda upp CSV", type=["csv"])
        for line in (pasted or "").splitlines():
            if line.strip():
                list_ids.append(line.strip())
        if up is not None:
            text = up.getvalue().decode("utf-8", errors="ignore")
            orgnrs = _re.findall(r"\b\d{6}-?\d{4}\b", text)
            list_ids.extend(orgnrs)
        list_ids = list(dict.fromkeys(list_ids))
        if list_ids:
            st.caption(f"📋 {len(list_ids)} bolag inlästa.")

    n_companies = st.slider("Max bolag att granska", 3, 300, 60,
                            help="Hur många bolag som djupgranskas (varulager). Fler = längre tid. 300 bolag ≈ 5–10 min.")

    # IHA-kriterierna ligger i en hopfällbar panel — standardvärdena passar de
    # flesta sök, så du slipper se rattarna varje gång. Öppna för att finjustera.
    st.subheader("2. Filter (IHA-kriterier)")
    with st.expander("⚙️ Justera filter (omsättning, storlek, lagerandel, marginal)",
                     expanded=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            oms_min = st.number_input("Omsättning min (MSEK)", 0.0, 1000.0, 50.0, step=10.0)
            oms_max = st.number_input("Omsättning max (MSEK)", 0.0, 5000.0, 300.0, step=10.0)
        with c2:
            max_anstallda = st.number_input("Max anställda", 1, 5000, 200, step=10)
            min_lagerandel = st.number_input("Min lagerandel (%)", 0.0, 100.0, 20.0, step=5.0)
        with c3:
            max_marginal = st.number_input("Max vinstmarginal (%)", -50.0, 50.0, 3.0, step=1.0)
            hard_margin = st.checkbox(
                "Marginal som hårt krav", value=False,
                help="Av (rekommenderas): låg marginal höjer bara prioriteten, men lönsamma bolag "
                     "med mycket lagerkapital tas ändå med. På: bara bolag under marginaltaket visas.",
            )
            st.caption("Lagerandel = varulager / omsättning. Marginal = resultat / omsättning.")
    st.caption(f"Aktivt filter: oms {oms_min:.0f}–{oms_max:.0f} MSEK · "
               f"≤{int(max_anstallda)} anställda · lagerandel ≥{min_lagerandel:.0f}% · "
               f"marginal ≤{max_marginal:.0f}%{' (hårt krav)' if hard_margin else ''}")

    can_run = bool(list_ids) if list_mode else True
    if st.button("🔎 Screena bolag", type="primary", disabled=not can_run):
        pool, found_n, in_bransch_n = [], 0, None

        if list_mode:
            pool = [{"_ident": x} for x in list_ids]
            found_n = len(list_ids)

        elif fritext.strip():
            # Nyckelordssökning — hämta bolag som matchar ordet, tillämpa ekonomifilter.
            kw = fritext.strip()
            ort_kw = ort if ort != "Hela Sverige" else "Sverige"
            with st.spinner(f"Söker '{kw}' på Allabolag..."):
                kw_hits = allabolag.search_companies(kw, ort_kw, max_results=50)
            pool = [c for c in kw_hits
                    if screener.passes_prefilter(
                        c, oms_min=oms_min, oms_max=oms_max,
                        max_anstallda=int(max_anstallda), max_marginal=max_marginal,
                        require_revenue=False, hard_margin=hard_margin)]
            found_n, in_bransch_n = len(kw_hits), len(pool)

        else:
            # Segmentering-svep län för län.
            if ort == "Hela Sverige":
                lan_list = screener.LAN
                per_lan = 150
            else:
                lan_list = [ort]
                per_lan = 300

            prefixes = screener.BRANSCH_SNI.get(bransch_val, [])
            pooled: dict = {}
            prog0 = st.progress(0.0, text=f"Sveper {len(lan_list)} län via Allabolag Segmentering...")
            for i, lan in enumerate(lan_list):
                try:
                    cands = allabolag.segmentering(
                        int(oms_min * 1000), int(oms_max * 1000),
                        location=lan, max_results=per_lan,
                    )
                except Exception:
                    cands = []
                for c in cands:
                    key = c.get("orgnr") or c.get("bolag", "").lower()
                    if key and key not in pooled:
                        pooled[key] = c
                prog0.progress((i + 1) / len(lan_list),
                               text=f"{lan}: {len(pooled)} bolag i bandet hittills...")

            found = list(pooled.values())
            in_bransch = [c for c in found
                          if screener.nace_matches(c.get("nace_code", ""), prefixes)]
            survivors = [c for c in in_bransch
                         if screener.passes_prefilter(
                             c, oms_min=oms_min, oms_max=oms_max,
                             max_anstallda=int(max_anstallda), max_marginal=max_marginal,
                             require_revenue=True, hard_margin=hard_margin)]

            def _margin(c):
                o, r = c.get("omsattning_msek"), c.get("resultat_msek")
                return (r / o * 100) if (o and r is not None) else 999
            survivors.sort(key=_margin)      # svagast lönsamhet först (bäst IHA-läge)
            pool = survivors
            found_n, in_bransch_n = len(found), len(in_bransch)

        # Cacha hela poolen + funnel så vi kan "granska fler" och om-filtrera utan
        # att svepa om. Djupläs sedan bara första batchen (slidern).
        st.session_state["screen_pool"] = pool
        st.session_state["screen_funnel"] = {
            "found": found_n, "in_bransch": in_bransch_n,
            "bransch_val": "" if (list_mode or fritext.strip()) else bransch_val,
        }
        batch = pool[:int(n_companies)]
        st.session_state["screen_fins"] = _deep_read_candidates(batch)
        st.session_state["screen_read"] = len(batch)
        _rescreen(oms_min, oms_max, max_anstallda, min_lagerandel, max_marginal, hard_margin)

    res = st.session_state.get("screen_result")
    if res:
        q_all = res["qualified"]
        try:
            _ex_names, _ex_orgnrs = db.get_excluded_identifiers()
            q = [
                r for r in q_all
                if r.get("bolag", "").lower() not in _ex_names
                and (not r.get("orgnr") or str(r.get("orgnr")) not in _ex_orgnrs)
            ]
            _hidden = len(q_all) - len(q)
        except Exception:
            q = q_all
            _hidden = 0
        st.divider()
        st.subheader(f"✅ {len(q)} kvalificerade bolag")
        if _hidden:
            st.caption(f"ℹ️ {_hidden} bolag dolda — finns redan i dina leads eller pipeline.")
        bransch_steg = (f"→ {res['in_bransch']} i rätt bransch "
                        if res.get("in_bransch") is not None else "")
        _pool_total = res.get("pool_total", 0)
        _read = res.get("read", 0)
        st.caption(f"{res.get('found', 0)} bolag i storleksbandet {bransch_steg}"
                   f"→ {_pool_total} klarade grovfiltret (storlek/marginal) "
                   f"→ **{_read} djuplästa** · {len(res['rejected'])} föll på lagerandel/storlek · "
                   f"{len(res['no_data'])} saknade data. "
                   f"Rankade på IHA-score (mest bundet kapital + svagast lönsamhet först).")

        # Diagnos: peka ut branschfiltret som flaskhals när det matchar nästan inget.
        _ib, _found = res.get("in_bransch"), res.get("found") or 0
        if (res.get("bransch_val") and _ib is not None and _found >= 50
                and _ib <= max(3, 0.03 * _found)):
            st.warning(
                f"⚠️ Branschfiltret **{res['bransch_val']}** matchade bara {_ib} av "
                f"{_found} bolag — **det är flaskhalsen, inte dina siffror**. Prova "
                f"**Alla lager-tunga branscher** för bredast nät, eller byt/utöka län.")

        # Fortsätt utan att svepa om: djupläs nästa batch, eller om-filtrera direkt.
        bc1, bc2 = st.columns(2)
        with bc1:
            _kvar = _pool_total - _read
            if _kvar > 0:
                if st.button(f"🔎 Granska fler bolag (nästa {min(int(n_companies), _kvar)} "
                             f"av {_kvar} kvar)", use_container_width=True):
                    nxt = st.session_state["screen_pool"][_read:_read + int(n_companies)]
                    st.session_state["screen_fins"] += _deep_read_candidates(nxt)
                    st.session_state["screen_read"] = _read + len(nxt)
                    _rescreen(oms_min, oms_max, max_anstallda, min_lagerandel,
                              max_marginal, hard_margin)
                    st.rerun()
            else:
                st.caption(f"✔️ Alla {_pool_total} bolag i bransch/band är djuplästa.")
        with bc2:
            if st.button("♻️ Uppdatera med nuvarande filter", use_container_width=True,
                         help="Kör om lagerandel/marginal-filtret på redan hämtade bolag "
                              "— direkt, ingen ny sökning."):
                _rescreen(oms_min, oms_max, max_anstallda, min_lagerandel,
                          max_marginal, hard_margin)
                st.rerun()

        if q:
            st.dataframe(pd.DataFrame([
                {
                    "Bolag": r["bolag"],
                    "Oms (MSEK)": r.get("omsattning_msek"),
                    "Anställda": r.get("anstallda"),
                    "Lagerandel %": r.get("lagerandel"),
                    "Marginal %": r.get("vinstmarginal"),
                    "IHA-score": r.get("iha_score"),
                    "Orgnr": r.get("orgnr"),
                } for r in q
            ]), use_container_width=True, hide_index=True)

            if st.button("💾 Spara kvalificerade som leads", type="primary"):
                records = []
                for r in q:
                    records.append({
                        "namn": "",
                        "titel": "Inköpschef",
                        "bolag": r["bolag"],
                        "bransch": r.get("bransch", ""),
                        "linkedin_url": "",
                        "website": r.get("website", ""),
                        "motivering": (f"Lagerandel {r.get('lagerandel')}%, marginal "
                                       f"{r.get('vinstmarginal')}%, oms {r.get('omsattning_msek')} MSEK "
                                       f"— mycket kapital bundet i lager."),
                        "score": int(r.get("iha_score") or 0),
                        "status": "pending",
                        "source": "allabolag",
                        "orgnr": r.get("orgnr"),
                        "omsattning": r.get("omsattning_msek"),
                        "resultat": r.get("resultat_msek"),
                        "anstallda": r.get("anstallda"),
                        "varulager": r.get("varulager_msek"),
                        "lagerandel": r.get("lagerandel"),
                        "vinstmarginal": r.get("vinstmarginal"),
                    })
                try:
                    existing = db.get_existing_companies()
                    fresh = [r for r in records if r["bolag"].lower() not in existing]
                    saved = db.insert_lead_suggestions(fresh)
                    st.success(f"Sparade {len(saved)} nya leads ({len(records) - len(fresh)} fanns redan). "
                               f"Gå till 🌱 Leads för att hitta personer & godkänna.")
                    st.button("🌱 Gå till Leads →", on_click=goto, args=("🌱 Leads",))
                except Exception as e:
                    st.error(f"Kunde inte spara: {e}")

        with st.expander("Se bortsorterade bolag (och varför)"):
            for r in res["rejected"]:
                st.caption(f"**{r['bolag']}** — {', '.join(r['skäl'])}")
