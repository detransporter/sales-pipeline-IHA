"""💰 Pipeline — affärssidan av tratten (deals med kontraktsvärde).

Prospects äger tratten FÖRE mötet (Leads → DM → svar), den här sidan äger den
EFTER: Meeting → IHA Proposal → Signed Contract / Lost. Deals skapas oftast
automatiskt när en kontakt får status mote_bokat, men kan också läggas in
manuellt (t.ex. affärer som inte kom via outreach-flödet).

Weighted pipeline räknar HELA tratten: även prospects i skickad/svar_ja syns
som förväntat värde (45 000 kr × sannolikhet), inte bara de sena affärerna.
"""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from database import supabase_client as db
from views import shared

STAGE_BADGE = {
    "Meeting": "🟡", "IHA Proposal": "🟠", "Signed Contract": "🟢", "Lost": "⚪",
}


def _deal_form(editing: dict | None = None):
    """Formulär för nytt/redigerat deal. Returnerar (values, save_clicked, cancel)."""
    e = editing or {}
    fk = st.session_state.get("deal_form_key", 0)
    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("Kontaktnamn *", value=e.get("contact_name") or "",
                             key=f"dl_name_{fk}")
        title = st.text_input("Titel", value=e.get("contact_title", "") or "",
                              key=f"dl_title_{fk}")
        company = st.text_input("Bolag *", value=e.get("company") or "",
                                key=f"dl_company_{fk}")
        email = st.text_input("E-post", value=e.get("email", "") or "",
                              key=f"dl_email_{fk}")
        phone = st.text_input("Telefon", value=e.get("phone", "") or "",
                              key=f"dl_phone_{fk}")
    with c2:
        stage = st.selectbox("Stage", db.DEAL_STAGES,
                             index=db.DEAL_STAGES.index(e["stage"])
                             if e.get("stage") in db.DEAL_STAGES else 0,
                             key=f"dl_stage_{fk}")
        value = st.number_input("Kontraktsvärde (kr)", 0, 10_000_000,
                                int(e.get("contract_value") or db.IHA_DEFAULT_VALUE),
                                step=5000, key=f"dl_value_{fk}")
        md_default = None
        if e.get("meeting_date"):
            try:
                md_default = datetime.strptime(str(e["meeting_date"])[:10], "%Y-%m-%d").date()
            except Exception:
                pass
        meeting_date = st.date_input("Mötesdatum", value=md_default,
                                     key=f"dl_meeting_{fk}")
        linkedin = st.text_input("LinkedIn-URL", value=e.get("linkedin_url", "") or "",
                                 key=f"dl_li_{fk}")
        is_partner = st.checkbox("Partner-deal (inte slutkund)",
                                 value=bool(e.get("is_partner")), key=f"dl_partner_{fk}")
    notes = st.text_area("Noteringar", value=e.get("notes", "") or "", height=80,
                         key=f"dl_notes_{fk}")

    b1, b2 = st.columns([2, 1])
    save = b1.button("💾 Spara deal" if not editing else "💾 Uppdatera deal",
                     type="primary", use_container_width=True, key=f"dl_save_{fk}")
    cancel = False
    if editing:
        cancel = b2.button("Avbryt redigering", use_container_width=True,
                           key=f"dl_cancel_{fk}")
    values = {
        "contact_name": name.strip(),
        "contact_title": title.strip(),
        "company": company.strip(),
        "email": email.strip() or None,
        "phone": phone.strip() or None,
        "linkedin_url": linkedin.strip() or None,
        "stage": stage,
        "contract_value": int(value),
        "meeting_date": meeting_date.isoformat() if meeting_date else None,
        "is_partner": is_partner,
        "notes": notes.strip(),
    }
    return values, save, cancel


def render():
    st.title("💰 Pipeline")
    st.caption("Affärerna efter bokat möte — Meeting → IHA Proposal → Signed. "
               "Deals skapas automatiskt när en kontakt får status mote_bokat.")

    deals = []
    with shared.action("Kunde inte läsa pipeline"):
        deals = db.fetch_pipeline_deals()

    # ── Nytt/redigera deal ────────────────────────────────────────────────────
    editing = st.session_state.get("deal_editing")
    with st.expander("➕ Nytt deal" if not editing
                     else f"✏️ Redigerar: {editing.get('contact_name', '')}",
                     expanded=bool(editing)):
        values, save, cancel = _deal_form(editing)
        if cancel:
            st.session_state["deal_editing"] = None
            st.session_state["deal_form_key"] = st.session_state.get("deal_form_key", 0) + 1
            st.rerun()
        if save:
            if not values["contact_name"] or not values["company"]:
                st.error("Kontaktnamn och bolag krävs.")
            else:
                with shared.action("Kunde inte spara", rerun=True):
                    if editing:
                        db.update_pipeline_deal(editing["id"], values)
                    else:
                        db.save_pipeline_deal(values)
                    st.session_state["deal_editing"] = None
                    st.session_state["deal_form_key"] = \
                        st.session_state.get("deal_form_key", 0) + 1
                    # Logga till Open Brain (minnet) — tåligt, får misslyckas
                    try:
                        from brain import open_brain
                        open_brain.capture_thought(
                            f"PIPELINE {'UPPDATERAT' if editing else 'NYTT'} DEAL – "
                            f"{datetime.utcnow().date().isoformat()}\n"
                            f"Kontakt: {values['contact_name']} ({values['company']})\n"
                            f"Stage: {values['stage']}\n"
                            f"Kontraktsvärde: {values['contract_value']:,} SEK"
                        )
                    except Exception:
                        pass
                    st.success("Sparat!")

    if not deals:
        st.info("Inga deals ännu. De skapas automatiskt vid mote_bokat, "
                "eller läggs in manuellt ovan.")
        return

    # ── KPI:er — hela tratten ────────────────────────────────────────────────
    df = pd.DataFrame(deals)
    signed = df[df["stage"] == "Signed Contract"]
    active = df[~df["stage"].isin(["Lost"])]
    deal_weighted = int(sum(
        (d.get("contract_value") or 0) * db.DEAL_PROB.get(d.get("stage", ""), 0) / 100
        for d in deals
    ))

    # Prospects-tratten: förväntat värde även FÖRE mötet
    prospect_weighted = 0
    try:
        prospects = db.get_prospects()
        for p in prospects:
            prob = db.PROSPECT_PROB.get(p.get("status", ""), 0)
            if prob:
                prospect_weighted += db.IHA_DEFAULT_VALUE * prob / 100
        prospect_weighted = int(prospect_weighted)
    except Exception:
        prospects = []

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Aktiva deals", len(active))
    k2.metric("Signerade", len(signed))
    k3.metric("Stängd intäkt", f"{int(signed['contract_value'].sum()):,} kr".replace(",", " "))
    k4.metric("Weighted pipeline", f"{deal_weighted + prospect_weighted:,} kr".replace(",", " "),
              help=f"Deals: {deal_weighted:,} kr + prospects i outreach: "
                   f"{prospect_weighted:,} kr (45 000 kr × sannolikhet per status)")

    # ── Funnel — hela tratten i en bild ──────────────────────────────────────
    try:
        n_skickad = sum(1 for p in prospects
                        if p.get("status") in ("skickad", "followup_1", "followup_2"))
        n_svar = sum(1 for p in prospects if p.get("status") == "svar_ja")
    except Exception:
        n_skickad, n_svar = 0, 0
    stage_counts = {s: 0 for s in db.DEAL_STAGES if s != "Lost"}
    for s in df["stage"]:
        if s in stage_counts:
            stage_counts[s] += 1

    funnel_labels = (["Kontaktad (5%)", "Svar ja (15%)"]
                     + [f"{s} ({db.DEAL_PROB[s]}%)" for s in stage_counts])
    funnel_values = [n_skickad, n_svar] + list(stage_counts.values())
    fig = go.Figure(go.Funnel(
        y=funnel_labels, x=funnel_values,
        textposition="inside", textinfo="value",
        marker=dict(color=["#94a3b8", "#60a5fa", "#f59e0b", "#fb923c", "#22c55e"]),
    ))
    fig.update_layout(title="Hela tratten: outreach → affär",
                      margin=dict(l=10, r=10, t=40, b=10), height=340)
    st.plotly_chart(fig, use_container_width=True)

    # ── Deal-lista med inline stage-flytt ────────────────────────────────────
    st.subheader("Alla deals")
    active_deals = [d for d in deals if d.get("stage") != "Lost"]
    lost_deals = [d for d in deals if d.get("stage") == "Lost"]

    def _deal_row(d: dict, i: int):
        with st.container(border=True):
            c_info, c_stage, c_edit = st.columns([4, 2, 1])
            with c_info:
                badge = STAGE_BADGE.get(d.get("stage", ""), "")
                val = int(d.get("contract_value") or 0)
                prob = db.DEAL_PROB.get(d.get("stage", ""), 0)
                st.markdown(f"**{d.get('contact_name', '')}** · {d.get('company', '')}  \n"
                            f"{badge} {d.get('stage', '')} · {val:,} kr "
                            f"(→ {int(val * prob / 100):,} kr viktat)".replace(",", " "))
                kontakt = " · ".join(x for x in [
                    d.get("email") or "", d.get("phone") or ""] if x)
                if kontakt:
                    st.caption(kontakt)
                if d.get("meeting_date"):
                    st.caption(f"📅 Möte: {str(d['meeting_date'])[:10]}")
                if d.get("notes"):
                    st.caption(f"📝 {d['notes'][:150]}")
            with c_stage:
                new_stage = st.selectbox(
                    "Stage", db.DEAL_STAGES,
                    index=db.DEAL_STAGES.index(d["stage"])
                    if d.get("stage") in db.DEAL_STAGES else 0,
                    key=f"stage_{d['id']}_{i}", label_visibility="collapsed")
                if new_stage != d.get("stage"):
                    with shared.action("Kunde inte flytta", rerun=True):
                        db.update_pipeline_deal(d["id"], {"stage": new_stage})
            with c_edit:
                if st.button("✏️", key=f"edit_{d['id']}_{i}", help="Redigera deal"):
                    st.session_state["deal_editing"] = d
                    st.session_state["deal_form_key"] = \
                        st.session_state.get("deal_form_key", 0) + 1
                    st.rerun()

    order = {s: i for i, s in enumerate(db.DEAL_STAGES)}
    for i, d in enumerate(sorted(active_deals,
                                 key=lambda x: order.get(x.get("stage", ""), 99),
                                 reverse=True)):
        _deal_row(d, i)

    if lost_deals:
        with st.expander(f"🗃️ Arkiv — Lost ({len(lost_deals)})"):
            for i, d in enumerate(lost_deals):
                _deal_row(d, 1000 + i)
