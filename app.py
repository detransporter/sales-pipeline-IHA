import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
import pandas as pd
import urllib.parse
from datetime import date

from utils.excel_parser import parse_excel, dataframe_to_prospect_records
from agents.prospecting import score_dataframe
from agents.dm_generator import generate_dm_variants, generate_followup
from agents.followup import get_followups_due, process_close, get_daily_summary
from agents.qualifier import qualify_reply, CATEGORY_TO_STATUS
from agents import orchestrator, learning, people_finder
from agents import inbox_watcher
from agents.orchestrator import ANGLE_TO_VARIANT
from database import supabase_client as db

st.set_page_config(
    page_title="Sales pipeline - IHA",
    page_icon="📊",
    layout="wide",
)


# ── Gemensamma hjälpare ─────────────────────────────────────────────────────

def linkedin_url_for(namn: str, bolag: str = "", url: str = "") -> tuple[str, str]:
    """
    Returnera (länktext, klickbar_url) för en person.
    Har vi en verifierad profil-URL → den. Annars en LinkedIn-personsökning på
    namn + bolag, så David kan hitta profilen och skicka invite manuellt.
    """
    url = (url or "").strip()
    if url:
        return "Öppna LinkedIn-profil", url
    keywords = " ".join(p for p in [(namn or "").strip(), (bolag or "").strip()] if p)
    search = ("https://www.linkedin.com/search/results/people/?keywords="
              + urllib.parse.quote(keywords))
    return "Sök personen på LinkedIn", search


def person_link_inline(namn: str, bolag: str = "", url: str = "") -> str:
    """Kompakt klickbar LinkedIn-länk som markdown-sträng (för listor)."""
    text, link = linkedin_url_for(namn, bolag, url)
    icon = "🔗" if (url or "").strip() else "🔎"
    return f"{icon} [{text}]({link})"


def goto(target: str) -> None:
    """Callback för navigeringsknappar — byter sida i menyn."""
    st.session_state["nav"] = target


def generate_best_dm(p: dict, best_variant: str = "variant_b") -> str:
    """Generera ETT DM (bästa vinkeln) för en kontakt, med ev. hemsidekontext."""
    website_context = ""
    if p.get("website"):
        try:
            from integrations import apify_research as _apify
            website_context = _apify.fetch_website_text(p["website"])
        except Exception:
            website_context = ""
    variants = generate_dm_variants(
        p.get("namn", ""), p.get("titel", ""), p.get("bolag", ""), p.get("bransch", ""),
        website_context=website_context,
    )
    return (variants.get(best_variant) or variants.get("variant_b")
            or variants.get("variant_a") or "")


def log_sent_email(prospect_id: str, to_addr: str, subject: str, body: str) -> None:
    """Logga ett skickat mejl i dm_history (typ='email') så det syns och inte dubbleras."""
    if not prospect_id:
        return
    try:
        dm = db.insert_dm(prospect_id, f"Till: {to_addr}\nÄmne: {subject}\n\n{body}",
                          typ="email")
        db.mark_dm_skickad(dm["id"])
    except Exception:
        pass


def render_email_composer(uid: str, to_default: str, draft_kwargs: dict,
                          to_options: list | None = None):
    """
    Visar mejl-komponenten (Till + 'Skriv utkast' + redigerbart ämne/text + skicka-knapp).
    Returnerar (to, subject, body, send_clicked). Genererar utkast bara på knapptryck
    (inte automatiskt) så vi inte drar API-anrop för varje kontakt på sidan.
    to_options: om fler adresser finns visas en selectbox istället för fritextfält.
    """
    from integrations import email_sender
    from agents import email_writer

    if not email_sender.is_configured():
        st.warning("Koppla din Gmail först — lägg `SMTP_USER` + `SMTP_PASS` i `.env` och starta "
                   "om appen. (Se 📊 Översikt → Skickade mejl för instruktion.)")
        return None, None, None, False

    opts = to_options or []
    if to_default and to_default not in opts:
        opts = [to_default] + opts
    opts = list(dict.fromkeys(o for o in opts if o))  # dedup, behåll ordning

    if len(opts) > 1:
        namn = draft_kwargs.get("namn", "")
        def _label(e):
            return f"{namn} — {e}" if namn else e
        st.selectbox("Till", opts, format_func=_label, key=f"to_{uid}")
    else:
        st.text_input("Till", value=opts[0] if opts else to_default, key=f"to_{uid}")
    if st.button("✍️ Skriv utkast", key=f"draft_{uid}"):
        with st.spinner("Skriver mejlutkast med bolagets lagersiffror..."):
            try:
                d = email_writer.generate_email(**draft_kwargs)
                st.session_state[f"subj_{uid}"] = d["subject"]
                st.session_state[f"body_{uid}"] = d["body"]
                st.session_state[f"draftdone_{uid}"] = True
            except Exception as e:
                st.error(f"Kunde inte skriva utkast: {e}")

    if st.session_state.get(f"draftdone_{uid}"):
        st.text_input("Ämne", key=f"subj_{uid}")
        st.text_area("Meddelande", key=f"body_{uid}", height=240)
        send = st.button("📨 Skicka mejl", key=f"sendmail_{uid}", type="primary")
        return (st.session_state.get(f"to_{uid}", to_default),
                st.session_state.get(f"subj_{uid}", ""),
                st.session_state.get(f"body_{uid}", ""), send)
    return st.session_state.get(f"to_{uid}", to_default), None, None, False


def render_company_analysis(a: dict) -> None:
    """Rendera en IHA-föranalys (från company_analyzer.analyze_company) snyggt."""
    tal = a.get("tal") or {}
    if tal:
        m1, m2, m3 = st.columns(3)
        m1.metric("Kapital i lager", f"{tal['varulager_msek']} MSEK")
        m2.metric("Årlig lagerkostnad (~20%)", f"{tal['arlig_lagerkostnad_msek']} MSEK")
        m3.metric("Frigörbart (uppskattat)",
                  f"{tal['frigorbart_lag_msek']}–{tal['frigorbart_hog_msek']} MSEK")
    if a.get("sammanfattning"):
        st.markdown(f"**Sammanfattning.** {a['sammanfattning']}")
    if a.get("varfor_passar"):
        st.markdown("**Varför bolaget passar IHA:**")
        for p in a["varfor_passar"]:
            st.markdown(f"- {p}")
    if a.get("potential"):
        st.markdown(f"**Potential.** {a['potential']}")
    if a.get("samtalskrokar"):
        st.markdown("**Samtalskrokar (öppningar):**")
        for h in a["samtalskrokar"]:
            st.markdown(f"- {h}")
    if a.get("riskflaggor"):
        st.markdown("**Att vara medveten om:**")
        for r in a["riskflaggor"]:
            st.caption(f"⚠️ {r}")


# ── Sidebar navigation (tratt-ordning + verktyg sist) ───────────────────────

st.sidebar.title("📊 Sales pipeline - IHA")
st.sidebar.divider()

PAGES = [
    "🏠 Idag",
    "🔍 Hitta bolag",
    "🌱 Leads",
    "💬 Svar & uppföljning",
    "📅 Möten",
    "📊 Översikt",
    "📥 Importera kontakter",
]

st.sidebar.caption("Arbetsflöde — uppifrån och ner")
page = st.sidebar.radio("Navigation", PAGES, key="nav", label_visibility="collapsed")

st.sidebar.divider()
try:
    stats = db.get_pipeline_stats()
    st.sidebar.metric("Kontaktade", stats["kontaktade"])
    st.sidebar.metric("Möten bokade", stats["moten"])
    st.sidebar.metric("Konvertering", f"{stats['konvertering']}%")
except Exception:
    st.sidebar.caption("_(Anslut Supabase för statistik)_")


# ══════════════════════════════════════════════════════════════════════════
# 🏠 Idag — startsida: visar vad som behöver göras och länkar dit
# ══════════════════════════════════════════════════════════════════════════

if page == "🏠 Idag":
    st.title("🏠 Idag")
    st.caption("Din dag i ordning. Varje kort visar vad som väntar — klicka för att gå dit.")

    try:
        state = orchestrator.gather_state()
    except Exception as e:
        st.error(f"Kunde inte läsa läget: {e}")
        st.stop()

    try:
        replies = db.get_inbox_replies(handled=False)
    except Exception:
        replies = []

    n_leads = len(state.get("pending_leads", []))
    n_dms = len(state.get("new_prospects", []))
    n_replies = len(replies)
    n_followups = len(state.get("followups", [])) + len(state.get("closes", []))
    n_meetings = len(state.get("summary", {}).get("meetings_today", []))

    # Steg-korten i tratt-ordning
    steps = [
        ("🔍", "Hitta bolag", "Sök fram nya bolag att kontakta", None, "🔍 Hitta bolag",
         "Öppna sök"),
        ("🌱", "Leads att godkänna", "Hitta person + godkänn", n_leads, "🌱 Leads",
         "Hantera leads"),
        ("💬", "Svar att hantera", "Svara på inkomna LinkedIn-svar", n_replies,
         "💬 Svar & uppföljning", "Öppna svar"),
        ("🔔", "Uppföljningar", "Följ upp / stäng kontakter", n_followups,
         "💬 Svar & uppföljning", "Öppna uppföljningar"),
        ("📅", "Möten idag", "Förbered dagens möten", n_meetings, "📅 Möten", "Öppna möten"),
    ]

    cols = st.columns(3)
    for i, (icon, titel, beskr, antal, target, knapp) in enumerate(steps):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"### {icon} {titel}")
                if antal is not None:
                    st.metric(label=beskr, value=antal)
                else:
                    st.caption(beskr)
                st.button(f"{knapp} →", key=f"go_{i}", on_click=goto, args=(target,),
                          use_container_width=True)

    st.divider()
    st.caption("Tips: följ korten i ordning — hitta bolag → godkänn leads → skicka DM → "
               "hantera svar → följ upp → boka möte.")


# ══════════════════════════════════════════════════════════════════════════
# 🔍 Hitta bolag — Allabolag-screener (oförändrad logik)
# ══════════════════════════════════════════════════════════════════════════

elif page == "🔍 Hitta bolag":
    st.title("🔍 Hitta bolag")
    st.caption("Hittar bolag där kapital bevisligen sitter fast i lager — via Allabolags "
               "publika siffror. Filtrera på storlek, lagerandel och lönsamhet, så får du "
               "en rankad lista att spara som leads.")

    from integrations import allabolag
    from agents import financial_screener as screener
    import re as _re

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

    n_companies = st.slider("Max bolag att granska", 3, 60, 20,
                            help="Hur många bolag som djupgranskas (varulager). Fler = längre tid.")

    st.subheader("2. Filter (IHA-kriterier)")
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
        else:
            # Sveper ETT län i taget och poolar (per-län-svep ger mångdubbelt fler i rätt
            # bransch än en nationell hämtning). Marginal mjuk (rankas via IHA-score).
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
        st.session_state["screen_result"] = res

    res = st.session_state.get("screen_result")
    if res:
        q = res["qualified"]
        st.divider()
        st.subheader(f"✅ {len(q)} kvalificerade bolag")
        bransch_steg = (f"→ {res['in_bransch']} i rätt bransch "
                        if res.get("in_bransch") is not None else "")
        st.caption(f"{res.get('found', 0)} bolag i storleksbandet {bransch_steg}"
                   f"→ {res.get('survivors', 0)} djuplästa (bokslut) · "
                   f"{len(res['rejected'])} föll på lagerandel/storlek · "
                   f"{len(res['no_data'])} saknade data. "
                   f"Rankade på IHA-score (mest bundet kapital + svagast lönsamhet först).")

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


# ══════════════════════════════════════════════════════════════════════════
# 🌱 Leads — hitta person + godkänn (en enda lista)
# ══════════════════════════════════════════════════════════════════════════

elif page == "🌱 Leads":
    st.title("🌱 Leads")
    st.caption("Sparade leads som väntar på person + godkännande. "
               "Hitta person → verifiera på LinkedIn → godkänn för att lägga i pipeline.")

    from integrations import apify_research as _apify
    from integrations import email_sender
    from agents import email_writer
    from agents import company_analyzer

    try:
        pending = db.get_lead_suggestions(status="pending")
    except Exception as e:
        pending = []
        st.error(f"Kunde inte läsa leads: {e}")

    # Hittade hemsidor/e-post under sessionen (visas även om DB saknar email-kolumn)
    contact_cache = st.session_state.setdefault("lead_contact", {})
    # Bolagsanalyser (genereras på knapptryck, cachas så vi inte drar API per omritning)
    analysis_cache = st.session_state.setdefault("lead_analysis", {})

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

                # Manuell kontaktregistrering (när automatik inte hittar rätt person).
                with st.expander("✏️ Lägg till kontakt manuellt"):
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

                # Bolagsanalys — ordentlig IHA-genomgång (siffror + hemsida) innan kontakt.
                with st.expander("📊 Analysera bolaget (IHA-föranalys)"):
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

                # Mejla direkt (backup-väg in om LinkedIn inte funkar). Att mejla = att
                # kontakta → leaden flyttas till pipeline (status 'skickad') och loggas.
                # Samla alla tillgängliga mejladresser: manuellt sparad + skrapad + gissad
                manual_email = l.get("email", "")
                all_emails = list(dict.fromkeys(
                    e for e in ([manual_email] + emails + ([guessed] if guessed else []))
                    if e
                ))
                if all_emails:
                    with st.expander("📧 Skriv & skicka mejl"):
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


# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# 💬 Svar & uppföljning — inkomna svar + uppföljningar (sammanslaget)
# ══════════════════════════════════════════════════════════════════════════

elif page == "💬 Svar & uppföljning":
    st.title("💬 Svar & uppföljning")

    tab_svar, tab_followup = st.tabs(["💬 Svar att hantera", "🔔 Uppföljningar"])

    # ── Tab 1: inkomna svar ────────────────────────────────────────────────
    with tab_svar:
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

    # ── Tab 2: uppföljningar ───────────────────────────────────────────────
    with tab_followup:
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


# ══════════════════════════════════════════════════════════════════════════
# 📅 Möten
# ══════════════════════════════════════════════════════════════════════════

elif page == "📅 Möten":
    st.title("📅 Möten")

    tab1, tab2 = st.tabs(["📋 Bokade möten", "➕ Boka nytt möte"])

    with tab1:
        try:
            meetings = db.get_meetings()
        except Exception as e:
            st.error(f"Fel: {e}")
            meetings = []

        if not meetings:
            st.info("Inga bokade möten.")
        else:
            for m in meetings:
                prospect_name = m.get("prospects", {}).get("namn", "Okänd") if m.get("prospects") else "Okänd"
                bolag = m.get("prospects", {}).get("bolag", "") if m.get("prospects") else ""
                with st.expander(f"{m['datum']} — {prospect_name} @ {bolag} [{m['status']}]"):
                    notes = st.text_area("Anteckningar", value=m.get("anteckningar") or "", key=f"notes_{m['id']}")
                    new_status = st.selectbox(
                        "Status",
                        ["bokad", "genomford", "avbokad"],
                        index=["bokad", "genomford", "avbokad"].index(m["status"]),
                        key=f"mstatus_{m['id']}",
                    )
                    if st.button("💾 Spara", key=f"save_meeting_{m['id']}"):
                        try:
                            db.update_meeting(m["id"], {"anteckningar": notes, "status": new_status})
                            st.success("Sparat!")
                        except Exception as e:
                            st.error(f"Fel: {e}")

    with tab2:
        st.subheader("Boka nytt möte")
        try:
            prospects_all = db.get_prospects()
            prospect_options = {f"{p['namn']} — {p['bolag']}": p for p in prospects_all}
        except Exception as e:
            st.error(f"Fel: {e}")
            prospect_options = {}

        if prospect_options:
            chosen_p = st.selectbox("Kontakt", list(prospect_options.keys()))
            meeting_date = st.date_input("Datum", value=date.today())
            if st.button("📅 Boka möte", type="primary"):
                try:
                    p = prospect_options[chosen_p]
                    db.insert_meeting(p["id"], meeting_date.isoformat())
                    db.update_prospect_status(p["id"], "mote_bokat")
                    st.success(f"Möte bokat med {p['namn']} den {meeting_date}!")
                except Exception as e:
                    st.error(f"Fel: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 📊 Översikt — pipeline, statistik och inlärning (verktyg)
# ══════════════════════════════════════════════════════════════════════════

elif page == "📊 Översikt":
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
    col1, col2 = st.columns(2)
    with col1:
        status_filter = st.selectbox(
            "Status",
            ["Alla", "ej_kontaktad", "skickad", "followup_1", "followup_2",
             "svar_ja", "svar_nej", "inget_svar", "mote_bokat", "avbojd"],
        )
    with col2:
        min_score = st.slider("Min. score", 0, 20, 0)

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
            new_status = st.selectbox(
                "Ny status",
                ["ej_kontaktad", "skickad", "followup_1", "followup_2", "svar_ja",
                 "svar_nej", "inget_svar", "mote_bokat", "avbojd"],
                index=["ej_kontaktad", "skickad", "followup_1", "followup_2", "svar_ja",
                       "svar_nej", "inget_svar", "mote_bokat", "avbojd"].index(
                    chosen.get("status", "ej_kontaktad"))
                if chosen.get("status") in ["ej_kontaktad", "skickad", "followup_1",
                                             "followup_2", "svar_ja", "svar_nej",
                                             "inget_svar", "mote_bokat", "avbojd"] else 0,
                key="status_pick"
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


# ══════════════════════════════════════════════════════════════════════════
# 📥 Importera kontakter
# ══════════════════════════════════════════════════════════════════════════

elif page == "📥 Importera kontakter":
    st.title("📥 Importera LinkedIn-kontakter")

    uploaded = st.file_uploader("Ladda upp Excel-fil (.xlsx)", type=["xlsx", "xls"])

    if uploaded:
        df, errors = parse_excel(uploaded)

        for err in errors:
            if "Varning" in err:
                st.warning(err)
            else:
                st.error(err)
                st.stop()

        st.success(f"{len(df)} kontakter laddade. Poängsätter...")

        df_scored = score_dataframe(df)
        st.info(f"{len(df_scored)} kontakter passerar minpoäng (≥5). "
                f"{len(df) - len(df_scored)} filtrerades bort.")

        display_cols = ["namn", "titel", "bolag", "bransch", "score"]
        available = [c for c in display_cols if c in df_scored.columns]
        st.dataframe(df_scored[available], use_container_width=True, hide_index=True)

        if st.button("💾 Spara till Supabase", type="primary"):
            records = dataframe_to_prospect_records(df_scored)
            for i, row in df_scored.iterrows():
                records[i]["score"] = int(row["score"])
            try:
                saved = db.insert_prospects(records)
                st.success(f"✅ {len(saved)} kontakter sparade till Supabase!")
            except Exception as e:
                st.error(f"Supabase-fel: {e}")
