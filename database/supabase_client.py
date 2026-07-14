import os
from supabase import create_client, Client, ClientOptions
from dotenv import load_dotenv

load_dotenv()

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        # Schema är styrbart via .env (default 'public'). Sätt SUPABASE_SCHEMA=sales_chief
        # för att köra mot det migrerade schemat i Open Brain. Alla anrop scopas då dit.
        schema = os.getenv("SUPABASE_SCHEMA", "public").strip() or "public"
        if not url or not key:
            raise ValueError("SUPABASE_URL och SUPABASE_KEY måste vara satta i .env")
        _client = create_client(url, key, options=ClientOptions(schema=schema))
    return _client


# ── Prospects ──────────────────────────────────────────────────────────────

# Valfria, nyare kolumner som en äldre databas kanske saknar. Skrivningar
# faller tillbaka utan dem (och den som anropar kan visa ett SQL-tips).
_OPTIONAL_COLS = ("kategori",)


def insert_prospects(records: list[dict]) -> list[dict]:
    client = get_client()
    result = client.table("prospects").insert(records).execute()
    return result.data


def insert_prospect(record: dict) -> dict:
    """
    Skapa EN kontakt manuellt. Tålig mot en databas som ännu saknar valfria
    kolumner (t.ex. 'kategori') — försöker då igen utan dem.
    """
    client = get_client()
    try:
        result = client.table("prospects").insert(record).execute()
        return result.data[0] if result.data else {}
    except Exception:
        slim = {k: v for k, v in record.items() if k not in _OPTIONAL_COLS}
        if slim == record:
            raise  # felet berodde inte på en valfri kolumn — låt det bubbla upp
        result = client.table("prospects").insert(slim).execute()
        return result.data[0] if result.data else {}


def get_prospects(status: str | None = None, min_score: int = 0) -> list[dict]:
    client = get_client()
    query = client.table("prospects").select("*").gte("score", min_score)
    if status:
        query = query.eq("status", status)
    result = query.order("score", desc=True).execute()
    return result.data


def update_prospect(prospect_id: str, fields: dict) -> dict:
    """Uppdatera valfria fält på en kontakt. Tålig mot saknade valfria kolumner."""
    client = get_client()
    try:
        result = client.table("prospects").update(fields).eq("id", prospect_id).execute()
        return result.data[0] if result.data else {}
    except Exception:
        slim = {k: v for k, v in fields.items() if k not in _OPTIONAL_COLS}
        if slim == fields or not slim:
            raise
        result = client.table("prospects").update(slim).eq("id", prospect_id).execute()
        return result.data[0] if result.data else {}


def delete_prospect(prospect_id: str) -> bool:
    """Ta bort en kontakt permanent (alla kopplade DM/svar tas bort via cascade i DB)."""
    client = get_client()
    client.table("prospects").delete().eq("id", prospect_id).execute()
    return True


def update_prospect_status(prospect_id: str, status: str) -> dict:
    client = get_client()
    result = client.table("prospects").update({"status": status}).eq("id", prospect_id).execute()
    prospect = result.data[0] if result.data else {}
    # Överlämning outreach → affär: bokat möte skapar ett deal i Meeting-stage
    # (om inget redan finns). Tåligt — får aldrig blockera statusuppdateringen.
    if status == "mote_bokat" and prospect:
        try:
            _promote_to_deal(prospect)
        except Exception:
            pass
    return prospect


def _promote_to_deal(prospect: dict) -> None:
    """Skapa ett deal (Meeting, 45 000 kr) från en prospect vid bokat möte."""
    pid = prospect.get("id")
    if not pid or deal_exists_for_prospect(pid):
        return
    save_pipeline_deal({
        "contact_name": prospect.get("namn") or prospect.get("bolag") or "?",
        "contact_title": prospect.get("titel") or "",
        "company": prospect.get("bolag") or "?",
        "email": prospect.get("email") or None,
        "phone": prospect.get("telefon") or None,
        "linkedin_url": prospect.get("linkedin_url") or None,
        "stage": "Meeting",
        "contract_value": IHA_DEFAULT_VALUE,
        "notes": (prospect.get("extra_info") or "")[:300],
        "prospect_id": pid,
    })


def update_prospect_stage(prospect_id: str, steg: str) -> dict:
    """Uppdatera var i säljtrappan (samtal_steg) en kontakt befinner sig."""
    client = get_client()
    result = client.table("prospects").update({"samtal_steg": steg}).eq("id", prospect_id).execute()
    return result.data[0] if result.data else {}


def get_prospect_by_name(name: str) -> dict | None:
    client = get_client()
    result = client.table("prospects").select("*").ilike("namn", f"%{name}%").limit(1).execute()
    return result.data[0] if result.data else None


# ── DM History ─────────────────────────────────────────────────────────────

def insert_dm(prospect_id: str, meddelande: str, typ: str = "initial", angle: str | None = None) -> dict:
    client = get_client()
    result = client.table("dm_history").insert({
        "prospect_id": prospect_id,
        "meddelande": meddelande,
        "typ": typ,
        "angle": angle,
        "status": "genererad",
    }).execute()
    return result.data[0] if result.data else {}


def mark_dm_skickad(dm_id: str, at: str | None = None) -> dict:
    """Markera skickad. `at` (ISO-datum) kan sättas till FRAMTID för att pausa
    uppföljningar — followup räknar dagar sedan senaste dm:ets skickad_at."""
    from datetime import datetime
    client = get_client()
    result = client.table("dm_history").update({
        "status": "skickad",
        "skickad_at": at or datetime.utcnow().isoformat(),
    }).eq("id", dm_id).execute()
    return result.data[0] if result.data else {}


def update_dm_svar(dm_id: str, status: str, svar_text: str = "") -> dict:
    from datetime import datetime
    client = get_client()
    result = client.table("dm_history").update({
        "status": status,
        "svar_at": datetime.utcnow().isoformat(),
        "svar_text": svar_text,
    }).eq("id", dm_id).execute()
    return result.data[0] if result.data else {}


def get_dm_history(prospect_id: str) -> list[dict]:
    client = get_client()
    result = client.table("dm_history").select("*").eq("prospect_id", prospect_id).order("created_at").execute()
    return result.data


def get_latest_dm(prospect_id: str) -> dict | None:
    client = get_client()
    result = (
        client.table("dm_history")
        .select("*")
        .eq("prospect_id", prospect_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ── Meetings ───────────────────────────────────────────────────────────────

def insert_meeting(prospect_id: str, datum: str) -> dict:
    client = get_client()
    result = client.table("meetings").insert({
        "prospect_id": prospect_id,
        "datum": datum,
        "status": "bokad",
    }).execute()
    return result.data[0] if result.data else {}


def get_meetings(status: str | None = None) -> list[dict]:
    client = get_client()
    query = client.table("meetings").select(
        "*, prospects(namn, bolag, titel, kategori)").order("datum")
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return result.data


def update_meeting(meeting_id: str, updates: dict) -> dict:
    client = get_client()
    result = client.table("meetings").update(updates).eq("id", meeting_id).execute()
    return result.data[0] if result.data else {}


# ── Stats ──────────────────────────────────────────────────────────────────

def get_pipeline_stats() -> dict:
    client = get_client()
    prospects = client.table("prospects").select("status").execute().data
    total = len(prospects)
    kontaktade = sum(1 for p in prospects if p["status"] not in ("ej_kontaktad",))
    svar = sum(1 for p in prospects if p["status"] in ("svar_ja", "svar_nej"))
    moten = sum(1 for p in prospects if p["status"] == "mote_bokat")
    konvertering = round(moten / kontaktade * 100, 1) if kontaktade > 0 else 0
    return {
        "totalt": total,
        "kontaktade": kontaktade,
        "svar": svar,
        "moten": moten,
        "konvertering": konvertering,
    }


# ── Orchestrator: körningar & logg ──────────────────────────────────────────

def start_run(run_type: str = "daily") -> dict:
    """Skapa en ny orchestrator-körning och returnera den."""
    client = get_client()
    result = client.table("agent_runs").insert({"run_type": run_type}).execute()
    return result.data[0] if result.data else {}


def finish_run(run_id: str, summary: dict) -> dict:
    from datetime import datetime
    client = get_client()
    result = client.table("agent_runs").update({
        "finished_at": datetime.utcnow().isoformat(),
        "summary": summary,
    }).eq("id", run_id).execute()
    return result.data[0] if result.data else {}


def log_action(run_id: str | None, agent: str, action: str,
               prospect_id: str | None = None, detail: dict | None = None) -> dict:
    """Logga en enskild agent-åtgärd för full spårbarhet."""
    client = get_client()
    result = client.table("agent_log").insert({
        "run_id": run_id,
        "agent": agent,
        "action": action,
        "prospect_id": prospect_id,
        "detail": detail,
    }).execute()
    return result.data[0] if result.data else {}


def get_recent_runs(limit: int = 10) -> list[dict]:
    client = get_client()
    result = (
        client.table("agent_runs")
        .select("*")
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


def get_run_log(run_id: str) -> list[dict]:
    client = get_client()
    result = (
        client.table("agent_log")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at")
        .execute()
    )
    return result.data


# ── Lead-förslag (lead_finder → David godkänner) ────────────────────────────

def insert_lead_suggestions(records: list[dict]) -> list[dict]:
    if not records:
        return []
    client = get_client()
    result = client.table("lead_suggestions").insert(records).execute()
    return result.data


def get_lead_suggestions(status: str | None = "pending") -> list[dict]:
    client = get_client()
    query = client.table("lead_suggestions").select("*")
    if status:
        query = query.eq("status", status)
    result = query.order("score", desc=True).execute()
    return result.data


def promote_lead(suggestion: dict) -> dict:
    """Gör ett godkänt lead-förslag till en riktig prospect och markera som approved."""
    record = {
        "namn": suggestion.get("namn") or "",
        "titel": suggestion.get("titel") or "",
        "bolag": suggestion.get("bolag") or "",
        "bransch": suggestion.get("bransch") or "",
        "linkedin_url": suggestion.get("linkedin_url") or "",
        "website": suggestion.get("website") or "",
        "extra_info": suggestion.get("motivering") or "",
        "score": int(suggestion.get("score") or 0),
        "status": "ej_kontaktad",
    }
    # Bär med ekonomiska nyckeltal + e-post om de finns
    for col in ("orgnr", "omsattning", "resultat", "anstallda",
                "varulager", "lagerandel", "vinstmarginal", "email", "telefon"):
        if suggestion.get(col) is not None:
            record[col] = suggestion[col]
    try:
        saved = insert_prospects([record])
    except Exception:
        # 'email'-kolumnen saknas kanske på prospects — försök utan den.
        record.pop("email", None)
        saved = insert_prospects([record])
    if suggestion.get("id"):
        update_lead_suggestion(suggestion["id"], "approved")
    return saved[0] if saved else {}


def get_sent_emails(limit: int = 100) -> list[dict]:
    """Lista skickade mejl (logg) med bolag/namn — för översikt och dedup."""
    client = get_client()
    result = (
        client.table("dm_history")
        .select("*, prospects(namn, bolag)")
        .eq("typ", "email")
        .eq("status", "skickad")
        .order("skickad_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


def has_sent_email(prospect_id: str) -> bool:
    """True om kontakten redan fått ett mejl (för att undvika dubbletter)."""
    if not prospect_id:
        return False
    client = get_client()
    result = (
        client.table("dm_history")
        .select("id")
        .eq("prospect_id", prospect_id)
        .eq("typ", "email")
        .eq("status", "skickad")
        .limit(1)
        .execute()
    )
    return bool(result.data)


def update_lead_suggestion_person(suggestion_id: str, namn: str,
                                  titel: str = "", linkedin_url: str = "") -> dict:
    """Spara person (namn + ev. titel/LinkedIn-URL) som people_finder hittade på ett lead."""
    updates = {"namn": namn}
    if titel:
        updates["titel"] = titel
    if linkedin_url:
        updates["linkedin_url"] = linkedin_url
    client = get_client()
    result = (
        client.table("lead_suggestions")
        .update(updates)
        .eq("id", suggestion_id)
        .execute()
    )
    return result.data[0] if result.data else {}


def update_lead_suggestion_contact(suggestion_id: str, email: str = "",
                                   website: str = "", telefon: str = "") -> bool:
    """
    Spara hittad e-post, hemsida och/eller telefon på ett lead. Tålig: okända
    kolumner hoppas över. Returnerar True om e-posten faktiskt sparades.
    """
    client = get_client()
    updates = {}
    if email:
        updates["email"] = email
    if website:
        updates["website"] = website
    if telefon:
        updates["telefon"] = telefon
    if not updates:
        return False
    try:
        client.table("lead_suggestions").update(updates).eq("id", suggestion_id).execute()
        return bool(email)
    except Exception:
        # Fallback: spara fält ett i taget och hoppa över kolumner som saknas.
        saved = False
        for key, val in updates.items():
            try:
                client.table("lead_suggestions").update(
                    {key: val}).eq("id", suggestion_id).execute()
                if key == "email":
                    saved = True
            except Exception:
                pass
        return saved


def update_lead_suggestion(suggestion_id: str, status: str) -> dict:
    client = get_client()
    result = (
        client.table("lead_suggestions")
        .update({"status": status})
        .eq("id", suggestion_id)
        .execute()
    )
    return result.data[0] if result.data else {}


def get_existing_companies() -> set[str]:
    """Bolag som redan finns i pipeline eller bland förslag — så vi inte dubblerar."""
    client = get_client()
    prospects = client.table("prospects").select("bolag").execute().data
    suggestions = client.table("lead_suggestions").select("bolag").execute().data
    names = {str(p.get("bolag", "")).strip().lower() for p in prospects}
    names |= {str(s.get("bolag", "")).strip().lower() for s in suggestions}
    names.discard("")
    return names


def get_excluded_identifiers() -> tuple[set[str], set[str]]:
    """Returnerar (kända bolagsnamn lower, kända orgnr) från prospects + lead_suggestions.
    Används för att filtrera bort redan kända bolag ur sökresultat innan visning."""
    client = get_client()
    p_rows = client.table("prospects").select("bolag,orgnr").execute().data
    s_rows = client.table("lead_suggestions").select("bolag,orgnr").execute().data
    names: set[str] = set()
    orgnrs: set[str] = set()
    for row in p_rows + s_rows:
        n = str(row.get("bolag") or "").strip().lower()
        o = str(row.get("orgnr") or "").strip()
        if n:
            names.add(n)
        if o:
            orgnrs.add(o)
    return names, orgnrs


# ── Inlärning: hämta DM-historik kopplad till utfall ────────────────────────

def get_all_dm_history(typ: str | None = None) -> list[dict]:
    client = get_client()
    query = client.table("dm_history").select("*")
    if typ:
        query = query.eq("typ", typ)
    result = query.order("created_at").execute()
    return result.data


def get_daily_activity(days: int = 14) -> list[dict]:
    """
    Antal utgående kontakter per dag de senaste `days` dagarna.

    Räknar varje faktiskt skickad rad i dm_history (mejl, DM, uppföljning, samtal)
    på datumet i `skickad_at`. Dagar utan aktivitet fylls med 0 så grafen blir tät.
    Returnerar [{"datum": "YYYY-MM-DD", "antal": n}, ...] äldst → nyast.
    """
    from datetime import datetime, timedelta, timezone

    client = get_client()
    # Bara skickade rader (skickad_at satt). Hämta datum + typ.
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (
            client.table("dm_history")
            .select("skickad_at, typ")
            .eq("status", "skickad")
            .gte("skickad_at", since)
            .execute()
        ).data
    except Exception:
        rows = []

    # Typer som INTE är en riktig kontakt (paus/autosvar) räknas inte som aktivitet.
    _ej_kontakt = {"uppskjuten", "autosvar", "pausad"}

    # Räkna per lokalt (svenskt) datum. skickad_at är UTC → +2h approx sommartid.
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("typ") in _ej_kontakt:
            continue
        ts = r.get("skickad_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt + timedelta(hours=2)  # grov Sverige-justering
            key = local.date().isoformat()
        except Exception:
            key = str(ts)[:10]
        counts[key] = counts.get(key, 0) + 1

    # Fyll varje dag i fönstret, även nolldagar.
    today = (datetime.now(timezone.utc) + timedelta(hours=2)).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"datum": d, "antal": counts.get(d, 0)})
    return out


# ── Eget minne (agent_memory) ───────────────────────────────────────────────

def insert_memory(content: str, tags: str | None = None) -> dict:
    client = get_client()
    result = client.table("agent_memory").insert({
        "content": content,
        "tags": tags,
    }).execute()
    return result.data[0] if result.data else {}


def list_memory(limit: int = 15) -> list[dict]:
    client = get_client()
    result = (
        client.table("agent_memory")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


def search_memory(query: str, limit: int = 15) -> list[dict]:
    client = get_client()
    result = (
        client.table("agent_memory")
        .select("*")
        .ilike("content", f"%{query}%")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


# ── Inkorg / inkommande svar ────────────────────────────────────────────────

def find_prospect_by_url(url: str) -> dict | None:
    """Matcha en LinkedIn-profil-URL mot en prospect (tål https/trailing slash)."""
    if not url:
        return None
    handle = url.rstrip("/").split("/")[-1].strip().lower()
    if not handle:
        return None
    client = get_client()
    result = (
        client.table("prospects")
        .select("*")
        .ilike("linkedin_url", f"%{handle}%")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def reply_exists(external_id: str | None, prospect_id: str | None, text: str) -> bool:
    """Har vi redan sparat det här svaret? (dedup) — på external_id eller prospect+text."""
    client = get_client()
    if external_id:
        r = client.table("inbox_replies").select("id").eq("external_id", external_id).limit(1).execute()
        if r.data:
            return True
    if prospect_id and text:
        r = (
            client.table("inbox_replies")
            .select("id")
            .eq("prospect_id", prospect_id)
            .eq("text", text)
            .limit(1)
            .execute()
        )
        if r.data:
            return True
    return False


def insert_inbox_reply(record: dict) -> dict:
    client = get_client()
    result = client.table("inbox_replies").insert(record).execute()
    return result.data[0] if result.data else {}


def get_inbox_replies(handled: bool = False) -> list[dict]:
    client = get_client()
    result = (
        client.table("inbox_replies")
        .select("*, prospects(namn, bolag, titel)")
        .eq("handled", handled)
        .order("received_at", desc=True)
        .execute()
    )
    return result.data


def get_replies_for_prospect(prospect_id: str) -> list[dict]:
    """Alla inkomna svar för en kontakt (hanterade som ohanterade) — för samtalsavskrift."""
    client = get_client()
    result = (
        client.table("inbox_replies")
        .select("*")
        .eq("prospect_id", prospect_id)
        .order("received_at")
        .execute()
    )
    return result.data


def mark_reply_handled(reply_id: str) -> dict:
    client = get_client()
    result = (
        client.table("inbox_replies")
        .update({"handled": True})
        .eq("id", reply_id)
        .execute()
    )
    return result.data[0] if result.data else {}


# ── Deal-pipeline (affärssidan efter bokat möte) ─────────────────────────────
# Prospects äger tratten FÖRE mötet; deals äger den EFTER. En kontakt lämnas
# över vid mote_bokat och spåras sedan här med kontraktsvärde + sannolikhet.

DEAL_STAGES = ["Meeting", "IHA Proposal", "Signed Contract", "Lost"]
DEAL_PROB = {"Meeting": 40, "IHA Proposal": 75, "Signed Contract": 100, "Lost": 0}

# Sannolikheter för prospects-statusar — gör weighted pipeline kontinuerlig
# över HELA tratten (outreach + affär).
PROSPECT_PROB = {"skickad": 5, "followup_1": 5, "followup_2": 5, "svar_ja": 15}
IHA_DEFAULT_VALUE = 45_000  # IHA Essential, förifyllt kontraktsvärde


def fetch_pipeline_deals() -> list[dict]:
    client = get_client()
    result = (
        client.table("pipeline")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


def save_pipeline_deal(row: dict) -> dict:
    client = get_client()
    result = client.table("pipeline").insert(row).execute()
    return result.data[0] if result.data else {}


def update_pipeline_deal(deal_id: int, updates: dict) -> dict:
    client = get_client()
    updates.setdefault("updated_at", datetime.utcnow().isoformat())
    result = (
        client.table("pipeline")
        .update(updates)
        .eq("id", deal_id)
        .execute()
    )
    return result.data[0] if result.data else {}


def deal_exists_for_prospect(prospect_id: str) -> bool:
    """True om kontakten redan har ett deal (så mote_bokat inte skapar dubbletter)."""
    if not prospect_id:
        return False
    client = get_client()
    result = (
        client.table("pipeline")
        .select("id")
        .eq("prospect_id", prospect_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def save_daily_reflection(raw_notes: str, summary: str) -> dict:
    client = get_client()
    result = client.table("daily_reflections").insert({
        "date": datetime.utcnow().date().isoformat(),
        "raw_notes": raw_notes,
        "summary": summary,
    }).execute()
    return result.data[0] if result.data else {}
