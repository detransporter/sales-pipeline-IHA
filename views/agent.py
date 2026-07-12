"""🧠 David Agent — dagcoach: dagstart → chatt → snapshot → kvällsreflektion.

Porterad från Sales.py (tab 8) men läser nu BÅDA världarna:
  - prospects-tratten (outreach: skickad/svar/möte) ur sales_chief
  - deal-pipelinen (affär: Meeting → IHA Proposal → Signed) ur sales_chief
  - Open Brain (berättande minne: tidigare chattar, kontaktnoter, reflektioner)

Agenten SKRIVER bara till Open Brain (chattloggar, reflektioner) — aldrig till
pipelinen. Supabase är den strukturerade sanningen, minnet ligger ovanpå.
"""

import os
import re
from datetime import date, datetime

import anthropic
import streamlit as st

from brain import open_brain
from database import supabase_client as db
from views import shared

MODEL = "claude-opus-4-6"   # dagstart + chatt (samma som i Sales.py)

DAVID_AGENT_SYSTEM = """Du är David Leifssons personliga assistent. Han är grundare av SCM International AB och Logistics Doctor i Västerås.

VEM DAVID ÄR:
- Supply chain-expert 20+ år, lageroptimering, DOS, reorder point
- Driver bolaget ensam, co-founder Lanny är bollplank
- Kommunikationsformel: DOS × inköpsvärde × antal = kapitalbindning × 20%

ARBETSSÄTT:
- Väljer uppgifter efter sannolikhet att lyckas
- Prokrastinerar med cold outreach – riskaversion
- Morgonen äts upp av scrollande – problemzon
- Energi från resultat och bokade möten

MÅL: 2 IHA Essential (45 000 kr/st) per månad – löpande, varje månad

AGENT-REGLER:
- Alltid konkret och handlingsorienterad
- Börja med mest sannolika vinsten idag
- Flagga prokrastinering
- Påminn om brottslingspunkten: 2 IHA/månad = frihet
- Max 40 min konsultjakt per dag
- Prata alltid svenska

TRATTEN (två delar, en helhet):
- OUTREACH (prospects): ej_kontaktad → skickad → svar_ja → mote_bokat
- AFFÄR (deals): Meeting (40%) → IHA Proposal (75%) → Signed Contract (100%)
Deals skapas när ett möte bokas. Din uppgift är att driva kontakter FRAMÅT i båda.

OPEN BRAIN:
Du får ibland ett avsnitt märkt "RELEVANT FRÅN OPEN BRAIN" i din kontext. Det är Davids sparade tankar, kontaktnoter och pipeline-historik. Använd alltid den informationen när den finns – det är ditt minne. Säg aldrig att du saknar tillgång till Open Brain om det avsnittet finns i din kontext."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _funnel_context() -> str:
    """Kompakt textbild av HELA tratten (prospects + deals) för prompts."""
    lines = []
    try:
        prospects = db.get_prospects()
        counts: dict[str, int] = {}
        for p in prospects:
            counts[p.get("status", "?")] = counts.get(p.get("status", "?"), 0) + 1
        outreach = ", ".join(f"{s}: {n}" for s, n in sorted(counts.items()) if n)
        lines.append(f"OUTREACH (prospects): {outreach or 'tomt'}")
    except Exception:
        lines.append("OUTREACH: (kunde inte läsas)")
    try:
        deals = db.fetch_pipeline_deals()
        dc: dict[str, int] = {}
        for d in deals:
            dc[d.get("stage", "?")] = dc.get(d.get("stage", "?"), 0) + 1
        deal_txt = ", ".join(f"{s}: {n}" for s, n in dc.items() if n)
        lines.append(f"AFFÄR (deals): {deal_txt or 'tomt'}")
        hot = [d for d in deals if d.get("stage") in ("Meeting", "IHA Proposal")]
        if hot:
            lines.append("HETA DEALS (kräver uppföljning):")
            for d in hot[:5]:
                lines.append(f"- {d.get('contact_name')} ({d.get('company', '')}) "
                             f"· {d.get('stage')} · {int(d.get('contract_value') or 0):,} kr")
    except Exception:
        lines.append("AFFÄR: (kunde inte läsas)")
    return "\n".join(lines)


def _parse_brain_chats_to_messages(raw: str) -> list[dict]:
    """Parsar AGENT CHATT-block från Open Brain till riktiga messages-par."""
    messages = []
    blocks = re.split(r'AGENT CHATT\s*[–-]\s*\d{4}-\d{2}-\d{2}', raw)
    for block in blocks:
        if not block.strip():
            continue
        david_m = re.search(r'David:\s*(.*?)(?=\nAgent:|\Z)', block, re.DOTALL)
        agent_m = re.search(r'Agent:\s*(.*?)$', block, re.DOTALL)
        if david_m and agent_m:
            d_text, a_text = david_m.group(1).strip(), agent_m.group(1).strip()
            if d_text and a_text:
                messages.append({"role": "user", "content": d_text})
                messages.append({"role": "assistant", "content": a_text})
    return messages[-6:] if len(messages) > 6 else messages


def render():
    st.title("🧠 David Agent")
    st.caption("Din dagcoach. Dagstart → chatt → pipeline-snapshot → kvällsreflektion.")

    for key, default in [
        ("agent_messages", []), ("agent_dagstart", None),
        ("agent_reflection_done", False), ("agent_memory_loaded", False),
        ("agent_brain_memory", ""), ("agent_memory_date", ""),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── 1. Dagstart ──────────────────────────────────────────────────────────
    st.subheader("☀️ Dagstart")
    c_start, c_reset = st.columns([3, 1])
    start_btn = c_start.button("☀️ Generera dagstart", use_container_width=True,
                               disabled=st.session_state["agent_dagstart"] is not None)
    if c_reset.button("🔄 Ny dagstart", use_container_width=True):
        st.session_state["agent_dagstart"] = None
        st.rerun()

    if start_btn:
        with st.spinner("Läser tratten + Open Brain och skriver briefing..."):
            with shared.action("Kunde inte generera dagstart", rerun=True):
                funnel = _funnel_context()
                brain_ctx = open_brain.search_thoughts("pipeline deal kontakt stage")
                brain_section = (f"\nPIPELINE-MINNE FRÅN OPEN BRAIN:\n{brain_ctx[:1200]}\n"
                                 if brain_ctx else "")
                veckodag = ["Måndag", "Tisdag", "Onsdag", "Torsdag",
                            "Fredag", "Lördag", "Söndag"][date.today().weekday()]
                prompt = (
                    f"Idag är {veckodag} {date.today().strftime('%d %B %Y')}.\n\n"
                    f"HELA TRATTEN:\n{funnel}\n{brain_section}\n"
                    "Generera en konkret dagbriefing baserad på HELA bilden ovan. "
                    "Max 200 ord. Inkludera:\n"
                    "1. Dagens viktigaste fokus (EN sak) — baserat på var de hetaste "
                    "kontakterna befinner sig\n"
                    "2. Konkret förstaåtgärd att göra inom 10 minuter\n"
                    "3. Påminnelse om målet: 2 IHA/månad = 90 000 kr/mån = frihet\n"
                    "4. En skarp fråga som utmanar David att inte prokrastinera"
                )
                resp = _client().messages.create(
                    model=MODEL, max_tokens=500,
                    system=DAVID_AGENT_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                st.session_state["agent_dagstart"] = resp.content[0].text

    if st.session_state["agent_dagstart"]:
        st.info(st.session_state["agent_dagstart"])
    else:
        st.caption("Tryck på knappen för att starta dagen.")

    st.divider()

    # ── 2. Chatt med minne ───────────────────────────────────────────────────
    st.subheader("💬 Dagsagent")

    # Ladda minne från Open Brain — en gång per dag
    today_str = date.today().isoformat()
    if st.session_state["agent_memory_date"] != today_str:
        st.session_state["agent_memory_loaded"] = False
        st.session_state["agent_memory_date"] = today_str
    if not st.session_state["agent_memory_loaded"]:
        recent = (open_brain.search_thoughts("AGENT CHATT pipeline")
                  or open_brain.list_thoughts(limit=10))
        st.session_state["agent_brain_memory"] = (recent or "")[:3000]
        st.session_state["agent_memory_loaded"] = True
        if recent and "AGENT CHATT" in recent and not st.session_state["agent_messages"]:
            hist = _parse_brain_chats_to_messages(recent)
            if hist:
                st.session_state["agent_messages"] = hist

    if (not st.session_state["agent_messages"]
            and st.session_state["agent_brain_memory"]):
        st.caption("🧠 Agenten har läst tidigare konversationer från Open Brain "
                   "— du behöver inte repetera.")

    for msg in st.session_state["agent_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    chat_input = st.chat_input("Ställ en fråga eller berätta vad du jobbar med...")
    if chat_input:
        st.session_state["agent_messages"].append({"role": "user", "content": chat_input})
        with st.chat_message("user"):
            st.markdown(chat_input)

        funnel = _funnel_context()
        brain_hit = open_brain.search_thoughts(chat_input)
        system_ctx = (DAVID_AGENT_SYSTEM
                      + f"\n\nHELA TRATTEN JUST NU:\n{funnel}"
                      + f"\nDATUM: {date.today().isoformat()}")
        if st.session_state["agent_dagstart"]:
            system_ctx += f"\nDAGSTART: {st.session_state['agent_dagstart'][:300]}"
        memory = st.session_state["agent_brain_memory"]
        if memory:
            system_ctx += f"\n\nTIDIGARE KONVERSATIONER & MINNEN (Open Brain):\n{memory}"
        if brain_hit and brain_hit != memory:
            system_ctx += f"\n\nRELEVANT FRÅN OPEN BRAIN (sökning):\n{brain_hit[:1500]}"

        with st.chat_message("assistant"):
            with st.spinner("..."):
                with shared.action("API-fel"):
                    resp = _client().messages.create(
                        model=MODEL, max_tokens=600,
                        system=system_ctx,
                        messages=st.session_state["agent_messages"],
                    )
                    reply = resp.content[0].text
                    st.markdown(reply)
                    st.session_state["agent_messages"].append(
                        {"role": "assistant", "content": reply})
                    open_brain.capture_thought(
                        f"AGENT CHATT – {date.today().isoformat()}\n\n"
                        f"David: {chat_input}\n\nAgent: {reply}")

    if st.session_state["agent_messages"]:
        if st.button("🗑️ Rensa chatt"):
            st.session_state["agent_messages"] = []
            st.rerun()

    st.divider()

    # ── 3. Snapshot — hela tratten kompakt ───────────────────────────────────
    st.subheader("🎯 Tratt-snapshot")
    try:
        deals = db.fetch_pipeline_deals()
        stats = db.get_pipeline_stats()
        hot = [d for d in deals if d.get("stage") in ("Meeting", "IHA Proposal")]
        signed = [d for d in deals if d.get("stage") == "Signed Contract"]
        weighted = int(sum(
            (d.get("contract_value") or 0) * db.DEAL_PROB.get(d.get("stage", ""), 0) / 100
            for d in deals))
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Kontaktade", stats.get("kontaktade", 0))
        s2.metric("Heta deals", len(hot))
        s3.metric("Signerade", len(signed))
        s4.metric("Weighted", f"{weighted:,} kr".replace(",", " "))
        if hot:
            st.markdown("**Kräver uppföljning:**")
            for d in hot:
                st.markdown(f"- **{d.get('contact_name')}** ({d.get('company', '')}) "
                            f"· {d.get('stage')} · "
                            f"{int(d.get('contract_value') or 0):,} kr".replace(",", " "))
        st.button("💰 Öppna Pipeline →", on_click=shared.goto, args=("💰 Pipeline",))
    except Exception as e:
        st.caption(f"Kunde inte läsa tratten: {e}")

    st.divider()

    # ── 4. Kvällsreflektion ──────────────────────────────────────────────────
    st.subheader("🌙 Kvällsreflektion")
    if st.session_state["agent_reflection_done"]:
        st.success("Dagens reflektion är sparad. Bra jobbat!")
        if st.button("✏️ Ny reflektion"):
            st.session_state["agent_reflection_done"] = False
            st.rerun()
    else:
        reflection = st.text_area(
            "Vad hände idag? Vad gick bra, vad gick sämre, vilka kontakter tog du?",
            height=140,
            placeholder="T.ex: Ringde Anna på Volvo Parts – positiv men inte redo än. "
                        "Skickade 3 mejl. Fastnade i inkorgen 2 timmar...")
        if st.button("🌙 Spara kvällsreflektion", use_container_width=True):
            if not reflection.strip():
                st.warning("Skriv något om dagen först.")
            else:
                with st.spinner("Sammanfattar och sparar..."):
                    with shared.action("Kunde inte spara reflektion"):
                        resp = _client().messages.create(
                            model=MODEL, max_tokens=300,
                            system=DAVID_AGENT_SYSTEM,
                            messages=[{"role": "user", "content": (
                                f"David har skrivit följande daganteckning:\n\n{reflection}\n\n"
                                "Skriv en kort sammanfattning (max 100 ord) på svenska med:\n"
                                "1. Vad som hände (fakta)\n2. En konkret lärdom\n"
                                "3. Rekommendation för imorgon")}],
                        )
                        summary = resp.content[0].text
                        db_ok, brain_ok = True, True
                        try:
                            db.save_daily_reflection(reflection.strip(), summary)
                        except Exception:
                            db_ok = False
                        brain_ok = open_brain.capture_thought(
                            f"DAGREFLEKTION {date.today().isoformat()}\n\n"
                            f"ANTECKNING:\n{reflection.strip()}\n\n"
                            f"SAMMANFATTNING:\n{summary}")
                        st.session_state["agent_reflection_done"] = True
                        st.markdown(f"**Sammanfattning:**\n\n{summary}")
                        st.caption("Sparad till: "
                                   + ("✅ Supabase" if db_ok else "⚠️ Supabase misslyckades")
                                   + " · "
                                   + ("✅ Open Brain" if brain_ok else "⚠️ Open Brain misslyckades"))
