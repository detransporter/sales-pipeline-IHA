"""🔍 Hitta bolag — Allabolag-screener (letar bundet lagerkapital)."""

import re as _re

import pandas as pd
import streamlit as st

from integrations import allabolag
from agents import financial_screener as screener
from database import supabase_client as db
from views.shared import goto


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
        found, seen = [], set()
        fins = []
        res_funnel = None

        if list_mode:
            ids = list_ids[:int(n_companies)]
            found = ids
            prog = st.progress(0.0, text="Läser bolagens ekonomi...")
            for i, ident in enumerate(ids):
                digits = _re.sub(r"\D", "", ident)
                try:
                    if len(digits) == 10:
                        fin = allabolag.get_financials(orgnr=digits)
                    else:
                        fin = allabolag.get_financials(company_name=ident)
                    if fin:
                        fins.append(fin)
                except Exception:
                    pass
                prog.progress((i + 1) / len(ids), text=f"Läst {i + 1}/{len(ids)} bolag")

        elif fritext.strip():
            # Nyckelordssökning — hämta bolag som matchar fritetxordet, tillämpa ekonomifilter.
            kw = fritext.strip()
            ort_kw = ort if ort != "Hela Sverige" else "Sverige"
            with st.spinner(f"Söker '{kw}' på Allabolag..."):
                kw_hits = allabolag.search_companies(kw, ort_kw, max_results=50)
            found = kw_hits
            st.caption(f"Hittade {len(kw_hits)} bolag för '{kw}' — tillämpar ekonomifilter...")

            survivors = [c for c in kw_hits
                         if screener.passes_prefilter(
                             c, oms_min=oms_min, oms_max=oms_max,
                             max_anstallda=int(max_anstallda), max_marginal=max_marginal,
                             require_revenue=False, hard_margin=hard_margin)]
            res_funnel = [len(kw_hits), len(survivors)]

            prog2 = st.progress(0.0, text="Läser varulager per bolag...")
            for i, c in enumerate(survivors[:int(n_companies)]):
                try:
                    fin = allabolag.get_financials(orgnr=c.get("orgnr", "")) if c.get("orgnr") else {}
                    if fin:
                        fin["website"] = fin.get("website") or c.get("website", "")
                        fin["bransch"] = fin.get("bransch") or c.get("bransch", "")
                        fins.append(fin)
                except Exception:
                    pass
                prog2.progress((i + 1) / max(1, min(len(survivors), int(n_companies))),
                               text=f"Läst {i + 1}/{min(len(survivors), int(n_companies))} bolag")

        else:
            # Segmentering-svep — nuvarande standardläge.
            if ort == "Hela Sverige":
                lan_list = screener.LAN
                per_lan = 150
            else:
                lan_list = [ort]
                per_lan = 300

            prefixes = screener.BRANSCH_SNI.get(bransch_val, [])
            pooled: dict = {}
            found = []
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
                found = list(pooled.values())
                prog0.progress((i + 1) / len(lan_list),
                               text=f"{lan}: {len(found)} bolag i bandet hittills...")

            in_bransch = [c for c in found
                          if screener.nace_matches(c.get("nace_code", ""), prefixes)]
            survivors = [c for c in in_bransch
                         if screener.passes_prefilter(
                             c, oms_min=oms_min, oms_max=oms_max,
                             max_anstallda=int(max_anstallda), max_marginal=max_marginal,
                             require_revenue=True, hard_margin=hard_margin)]
            res_funnel = (len(found), len(in_bransch), len(survivors))

            def _margin(c):
                o, r = c.get("omsattning_msek"), c.get("resultat_msek")
                return (r / o * 100) if (o and r is not None) else 999
            survivors.sort(key=_margin)
            survivors = survivors[:int(n_companies)]

            prog2 = st.progress(0.0, text="Läser varulager per bolag...")
            for i, c in enumerate(survivors):
                try:
                    fin = allabolag.get_financials(orgnr=c.get("orgnr", "")) if c.get("orgnr") else {}
                    if fin:
                        fin["website"] = fin.get("website") or c.get("website", "")
                        fin["bransch"] = fin.get("bransch") or c.get("bransch", "")
                        fins.append(fin)
                except Exception:
                    pass
                prog2.progress((i + 1) / max(1, len(survivors)),
                               text=f"Läst {i + 1}/{len(survivors)} bolag")

        res = screener.screen_companies(
            fins, oms_min=oms_min, oms_max=oms_max, max_anstallda=int(max_anstallda),
            min_lagerandel=min_lagerandel, max_marginal=max_marginal, hard_margin=hard_margin,
        )
        res["found"] = len(found)
        res["survivors"] = len(fins)
        res["in_bransch"] = res_funnel[1] if res_funnel else None
        # Spara vald bransch (bara relevant i segmenterings-läget) för diagnostik.
        res["bransch_val"] = "" if (list_mode or fritext.strip()) else bransch_val
        st.session_state["screen_result"] = res

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
        st.caption(f"{res.get('found', 0)} bolag i storleksbandet {bransch_steg}"
                   f"→ {res.get('survivors', 0)} djuplästa (bokslut) · "
                   f"{len(res['rejected'])} föll på lagerandel/storlek · "
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
