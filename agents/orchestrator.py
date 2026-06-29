"""
Orchestrator — "Sales Chief".

Den koordinerande agenten som håller ihop dagen så David bara behöver utföra,
inte planera. Varje körning:

  1. Läser läget   — pipeline från Supabase
  2. Lär sig       — vad funkar (inlärnings-agenten)
  3. Förbereder    — genererar DM:s för nya kontakter (med lärdomar), plockar fram
                     uppföljningar som förfallit, föreslår nya leads
  4. Loggar allt   — till Supabase (agent_runs/agent_log) + Open Brain (minnet)
  5. Sammanfattar  — en prioriterad lista redo att puttas till Telegram

Designprincip: Supabase = strukturerad sanning. Open Brain = berättande minne.
Allt är tålig — om Open Brain saknas fortsätter körningen ändå.
"""

from datetime import datetime, timezone

from database import supabase_client as db
from brain import memory as brain   # eget minne i din Supabase (byt till open_brain om du vill använda Open Brain igen)
from agents import learning
from agents.dm_generator import generate_dm_variants
from agents.followup import get_followups_due, get_daily_summary
from agents.lead_finder import suggest_leads
from integrations import apify_research as apify

# Hur mycket orchestratorn förbereder per körning (David vill max 40 min/dag)
DEFAULT_NEW_DMS = 5
DEFAULT_NEW_LEADS = 5

ANGLE_TO_VARIANT = {"a": "variant_a", "b": "variant_b", "c": "variant_c"}


# ── Lägesbild (snabb, läs-bara — används av UI och plan) ────────────────────

def gather_state() -> dict:
    """Snabb ögonblicksbild av pipeline utan att skapa något."""
    summary = get_daily_summary()
    stats = db.get_pipeline_stats()
    due = get_followups_due()
    followups = [d for d in due if d["action"] in ("followup_1", "followup_2")]
    closes = [d for d in due if d["action"] == "close"]
    new_prospects = db.get_prospects(status="ej_kontaktad", min_score=5)
    pending_leads = db.get_lead_suggestions(status="pending")
    return {
        "stats": stats,
        "summary": summary,
        "followups": followups,
        "closes": closes,
        "new_prospects": new_prospects,
        "pending_leads": pending_leads,
    }


def plan_day() -> dict:
    """Läs-bar plan: vad borde göras idag, utan att förbereda något."""
    state = gather_state()
    s = state["summary"]
    priorities = []
    if s["followups_due"]:
        priorities.append(f"Följ upp {s['followups_due']} kontakter som väntat klart")
    if s["new_to_send"]:
        priorities.append(f"Skicka DM till upp till {DEFAULT_NEW_DMS} nya kontakter")
    if state["closes"]:
        priorities.append(f"Stäng {len(state['closes'])} kontakter utan svar")
    if s["meetings_today"]:
        priorities.append(f"{len(s['meetings_today'])} möte(n) idag — förbered")
    if not priorities:
        priorities.append("Pipeline tunn — godkänn nya leads så fyller vi på")
    return {"state": state, "priorities": priorities}


# ── Körning: förbered faktiskt jobb ─────────────────────────────────────────

def run_day(run_type: str = "manual",
            n_new_dms: int = DEFAULT_NEW_DMS,
            n_leads: int = DEFAULT_NEW_LEADS,
            generate_dms: bool = True,
            find_leads: bool = True,
            lead_focus: str = "") -> dict:
    """
    Kör en hel orchestrering. Returnerar ett resultat-dict med allt förberett
    plus en Telegram-färdig text (result['telegram']).
    """
    run = db.start_run(run_type)
    run_id = run.get("id")
    today = datetime.now(timezone.utc).date().isoformat()

    # 1. Lär av historiken
    insight = learning.analyze_what_works()
    db.log_action(run_id, "learning", "Analyserade vad som funkar",
                  detail={"best_angle": insight["best_angle"],
                          "total_decided": insight["total_decided"]})

    state = gather_state()

    prepared_dms: list[dict] = []
    prepared_followups: list[dict] = []
    new_leads: list[dict] = []

    # 2. Förbered DM:s för nya kontakter (med lärdomar)
    if generate_dms:
        prepared_dms = _prepare_new_dms(run_id, state["new_prospects"],
                                        insight, n_new_dms)

    # 3. Plocka fram uppföljningar som förfallit (meddelanden redan färdiga)
    for item in state["followups"]:
        p = item["prospect"]
        prepared_followups.append({
            "prospect_id": p["id"], "namn": p["namn"], "bolag": p["bolag"],
            "action": item["action"], "message": item["message"],
        })
        db.log_action(run_id, "followup", f"Uppföljning klar: {item['action']}",
                      prospect_id=p["id"])

    # 4. Föreslå nya leads
    if find_leads:
        new_leads = _find_new_leads(run_id, n_leads, lead_focus)

    # 5. Sammanställ
    summary = {
        "date": today,
        "run_type": run_type,
        "stats": state["stats"],
        "best_angle": insight["best_angle"],
        "what_works": insight["brief"],
        "prepared_dms": len(prepared_dms),
        "prepared_followups": len(prepared_followups),
        "closes_due": len(state["closes"]),
        "new_leads": len(new_leads),
        "meetings_today": len(state["summary"]["meetings_today"]),
    }
    db.finish_run(run_id, summary)

    # 6. Skriv en sammanfattning till minnet (Open Brain)
    _remember(today, summary, insight)

    result = {
        "run_id": run_id,
        "summary": summary,
        "insight": insight,
        "prepared_dms": prepared_dms,
        "prepared_followups": prepared_followups,
        "new_leads": new_leads,
        "closes": state["closes"],
        "meetings_today": state["summary"]["meetings_today"],
    }
    result["telegram"] = build_telegram_text(result)
    return result


def _prepare_new_dms(run_id, new_prospects, insight, n_new_dms) -> list[dict]:
    """Generera och spara ett DM per ny kontakt, vinkeln vald av inlärningen."""
    prepared = []
    best_angle = insight["best_angle"]
    best_variant = ANGLE_TO_VARIANT.get(best_angle, "variant_b")

    # Filtrera bort kontakter som redan har ett DM FÖRST — annars äter de upp budgeten
    # och vi får färre (eller noll) nya DM trots att det finns gott om kontakter kvar.
    fresh_prospects = []
    for p in new_prospects:
        try:
            if db.get_latest_dm(p["id"]):
                continue
        except Exception:
            pass
        fresh_prospects.append(p)
        if len(fresh_prospects) >= n_new_dms:
            break

    for p in fresh_prospects[:n_new_dms]:

        # Personlig kontext från bolagets hemsida (research-agenten), om vi har den
        website_context = ""
        if p.get("website"):
            try:
                website_context = apify.fetch_website_text(p["website"])
            except Exception:
                website_context = ""

        try:
            variants = generate_dm_variants(
                p["namn"], p.get("titel", ""), p.get("bolag", ""),
                p.get("bransch", ""), extra_guidance=insight["brief"],
                website_context=website_context,
            )
        except Exception as e:
            db.log_action(run_id, "dm_generator", f"DM-fel för {p['namn']}: {e}",
                          prospect_id=p["id"])
            continue

        message = variants.get(best_variant) or variants.get("variant_b") or ""
        if not message:
            continue

        try:
            db.insert_dm(p["id"], message, typ="initial", angle=best_angle)
        except Exception:
            pass

        db.log_action(run_id, "dm_generator", "DM genererat & sparat",
                      prospect_id=p["id"], detail={"angle": best_angle})
        prepared.append({
            "prospect_id": p["id"], "namn": p["namn"], "titel": p.get("titel", ""),
            "bolag": p.get("bolag", ""), "score": p.get("score", 0),
            "linkedin_url": p.get("linkedin_url", ""),
            "angle": best_angle, "message": message, "variants": variants,
        })
    return prepared


def _find_new_leads(run_id, n_leads, lead_focus: str = "") -> list[dict]:
    try:
        existing = db.get_existing_companies()
        suggestions = suggest_leads(n=n_leads, existing_companies=existing, focus=lead_focus)
    except Exception as e:
        db.log_action(run_id, "lead_finder", f"Lead-fel: {e}")
        return []
    if not suggestions:
        return []
    for s in suggestions:
        s["run_id"] = run_id
    try:
        saved = db.insert_lead_suggestions(suggestions)
    except Exception:
        saved = suggestions
    db.log_action(run_id, "lead_finder", f"Föreslog {len(saved)} nya leads",
                  detail={"bolag": [s.get("bolag") for s in saved]})
    return saved


def _remember(today, summary, insight) -> None:
    """Spara en berättande sammanfattning till det egna minnet (agent_memory)."""
    content = (
        f"ORCHESTRATOR-KÖRNING – {today}\n"
        f"Förberedda DM: {summary['prepared_dms']} | "
        f"Uppföljningar: {summary['prepared_followups']} | "
        f"Att stänga: {summary['closes_due']} | "
        f"Nya leads: {summary['new_leads']} | "
        f"Möten idag: {summary['meetings_today']}\n"
        f"Bästa vinkel just nu: {summary['best_angle']}\n"
        f"Vad funkar: {insight['brief']}\n"
        f"Pipeline: {summary['stats']['kontaktade']} kontaktade, "
        f"{summary['stats']['moten']} möten, "
        f"{summary['stats']['konvertering']}% konvertering."
    )
    try:
        brain.capture_thought(content)
    except Exception:
        pass


# ── Telegram-text ───────────────────────────────────────────────────────────

def build_telegram_text(result: dict) -> str:
    s = result["summary"]
    lines = [f"🧠 *Sales Chief — {s['date']}*", ""]

    lines.append("*Dagens prioriteringar:*")
    if result["prepared_followups"]:
        lines.append(f"📬 Följ upp {len(result['prepared_followups'])} kontakter (DM färdiga)")
    if result["prepared_dms"]:
        lines.append(f"✉️ {len(result['prepared_dms'])} nya DM förberedda — skicka & markera")
    if result["closes"]:
        lines.append(f"🔒 Stäng {len(result['closes'])} utan svar")
    if result["meetings_today"]:
        lines.append(f"📅 {len(result['meetings_today'])} möte(n) idag")
    if result["new_leads"]:
        lines.append(f"🌱 {len(result['new_leads'])} nya leads att godkänna")
    if len(lines) == 3:
        lines.append("Inget akut — godkänn nya leads så fyller vi pipeline.")

    if result["prepared_dms"]:
        lines.append("")
        lines.append("*Nya kontakter att DM:a:*")
        for d in result["prepared_dms"][:5]:
            lines.append(f"• {d['namn']} — {d['titel']} @ {d['bolag']} (score {d['score']})")

    if result["new_leads"]:
        lines.append("")
        lines.append("*Föreslagna leads:*")
        for l in result["new_leads"][:5]:
            lines.append(f"• {l.get('bolag')} — {l.get('titel')} (score {l.get('score', 0)})")

    lines.append("")
    lines.append(f"_Vinkel som funkar: {s['best_angle']} • "
                 f"{s['stats']['moten']} möten / {s['stats']['konvertering']}% konv._")
    return "\n".join(lines)
