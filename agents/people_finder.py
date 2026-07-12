"""
People-finder — hittar rätt PERSON att kontakta på ett bolag, säkert och gratis(-ish).

Källor (i fallande kostnadsordning):
  1. Bolagets egen hemsida (GRATIS, ingen LinkedIn) — team-/kontakt-/om-oss-sidor.
  2a. Google → publika LinkedIn-profiler (via Apify) — kostar krediter. Används
     bara om Apify är konfigurerat OCH det finns krediter kvar (samma tröskel som
     leads.py visar kreditvarning vid, ~$0.50).
  2b. Claude web search (GRATIS från Apify-krediter sett; drar Anthropic-sökningar)
     — fallback när Apify saknas, krediterna är slut, eller Apify-vägen gav tomt.

Claude väger ihop källorna och pekar ut EN bästa person (namn + roll + ev. LinkedIn-URL).
Den hittar aldrig på personer — saknas data returneras tomt. David verifierar alltid
profilen innan kontakt.

Open Brain-inlärning (tåligt — fel loggar aldrig sönder sökningen):
  - Före sökning: hämtar tidigare lärdomar om personsökning för branschen.
  - Efter sökning: sparar en kort notering (bolag, metod, om person hittades).
  - Rättningar fångas i views/leads.py (när David skriver över en felgissning).
Open Brain nås via brain/open_brain.py (JSON-RPC över HTTP, OPEN_BRAIN_URL/KEY) —
INTE via MCP; det körande Streamlit-appen kan inte anropa MCP-verktyg.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

from integrations import apify_research as apify

# Open Brain-klient (tålig import — appen funkar även utan minnet).
try:
    from brain import open_brain as brain
except Exception:  # pragma: no cover
    brain = None

load_dotenv()

MODEL = "claude-sonnet-4-6"          # samma som lead_finder.py; stödjer web_search_20260209

# Under denna kreditnivå hoppar vi över den betalda Apify-vägen och kör gratis
# web search istället — samma tröskel som kreditvarningen i views/leads.py.
CREDIT_THRESHOLD = 0.50

# Roller värda att kontakta för IHA (svenska + engelska sökord till Google).
DEFAULT_ROLES = [
    "inköpschef", "logistikchef", "supply chain", "operations manager",
    "lagerchef", "ekonomichef", "CFO", "VD",
]


SYSTEM = """Du hjälper David Leifsson (Logistics Doctor) att hitta RÄTT person att kontakta på ett
bolag för att sälja IHA (lager-/kapitalbindningsanalys). Du får källor: text från bolagets
hemsida och/eller publika LinkedIn-/webbträffar. Peka ut EN bästa person.

PRIORITERA roller som äger lager/inköp/logistik/drift/ekonomi:
Supply Chain Manager, Inköpschef, Logistikchef, Operations Manager, Lagerchef, COO,
CFO/Ekonomichef, och i mindre bolag VD. Undvik HR, marknad, sälj, IT.

HÅRDA REGLER:
- Hitta ALDRIG på en person. Använd bara namn som faktiskt står i källorna.
- Om en LinkedIn-träff används: kopiera URL:en EXAKT, ändra den aldrig.
- Hittar du bara ett namn på hemsidan utan LinkedIn-URL: returnera namnet med tom URL.
- Hittar du ingen lämplig person alls: returnera "namn": "".
- LinkedIn-titlar ser ofta ut som "Namn Efternamn - Roll - Bolag | LinkedIn" — plocka isär det.

Returnera ENDAST JSON:
{
  "namn": "För- och efternamn, eller tom sträng",
  "titel": "personens roll",
  "linkedin_url": "exakt URL om känd, annars tom sträng",
  "kalla": "hemsida | linkedin | webbsök | ingen",
  "sakerhet": "hög | medel | låg",
  "motivering": "kort: varför denna person"
}
Ingen text utanför JSON."""


def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        # Sista utväg: plocka ut första {...}-blocket ur ev. omgivande text.
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                pass
        return {}


def _log(msg: str) -> None:
    """Enkel konsollogg så David ser vilken väg som kördes (appen har ingen logging-setup)."""
    print(msg)


# ── Open Brain-inlärning (tålig — fel får aldrig stoppa sökningen) ──────────────

def _recall_strategy(bransch: str, bolag: str) -> str:
    """Hämta tidigare lärdomar om personsökning för liknande bolag. '' om inget/fel."""
    if not brain or not brain.is_configured():
        return ""
    try:
        query = (f"people_finder strategi kontaktperson {bransch} SME "
                 f"var ledning/inköp finns").strip()
        res = brain.search_thoughts(query)
        return (res or "").strip()[:800]
    except Exception:
        return ""


def _remember_outcome(bolag: str, bransch: str, method: str,
                      found_namn: str, titel: str = "") -> None:
    """Spara en kort notering om utfallet (för framtida sökningar). Tyst vid fel."""
    if not brain or not brain.is_configured():
        return
    try:
        if found_namn:
            body = (f"[people_finder] {bolag} ({bransch or 'okänd bransch'}): via {method} "
                    f"→ hittade {found_namn}"
                    + (f", {titel}" if titel else "") + ".")
        else:
            body = (f"[people_finder] {bolag} ({bransch or 'okänd bransch'}): via {method} "
                    f"→ hittade ingen tydlig person. Prova annan roll/källa nästa gång.")
        brain.capture_thought(body[:400])
    except Exception:
        pass


# ── Kreditkoll (samma logik som views/leads.py) ────────────────────────────────

def _apify_credits_ok() -> bool:
    """True om Apify-vägen får köras (okänt saldo = tillåt; känt lågt = hoppa över)."""
    try:
        cred = apify.remaining_usage_usd()
    except Exception:
        cred = None
    return (cred is None) or (cred >= CREDIT_THRESHOLD)


# ── Källa 2b: Claude web search (gratis från Apify-krediter sett) ───────────────

def _web_search_person(bolag: str, target_role: str, bransch: str,
                       website_text: str, strategy: str) -> dict:
    """
    Leta rätt person via Claudes inbyggda web search-verktyg (bolagets Om oss/Ledning
    + publika LinkedIn). Returnerar {namn, titel, linkedin_url} eller {} vid fel.
    """
    roles = target_role or "VD/CEO, CFO/Ekonomichef, Inköpschef eller Supply Chain Manager"
    web_block = (f"\n\nTEXT FRÅN BOLAGETS HEMSIDA (utgångspunkt):\n{website_text[:2500]}"
                 if website_text else "")
    strat_block = (f"\n\nMINNE — tidigare lärdomar (använd bara om relevant):\n{strategy}"
                   if strategy else "")
    user = (
        f"Hitta rätt person att kontakta på bolaget \"{bolag}\" (bransch: {bransch or 'okänd'}) "
        f"för att sälja IHA (lager-/kapitalbindningsanalys).\n"
        f"Roll vi helst vill nå: {roles}. Prioritering: VD/CEO > CFO/Ekonomichef "
        f"> Inköpschef/Supply Chain Manager > Logistik-/Lagerchef.\n"
        f"Sök på webben: bolagets egen 'Om oss'/'Ledning'/'Team'/'Kontakt'-sida och "
        f"publika LinkedIn-profiler för personer på just detta bolag. "
        f"Hitta ALDRIG på en person — hittar du ingen tydlig, returnera tom sträng i \"namn\"."
        f"{web_block}{strat_block}\n\n"
        f"Returnera ENDAST JSON: "
        f'{{"namn": "...", "titel": "...", "linkedin_url": "...", "sakerhet": "hög|medel|låg", '
        f'"motivering": "kort"}}'
    )
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        # håll antalet sökningar lågt (kostnad) — max 3 per bolag
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
        messages = [{"role": "user", "content": user}]
        resp = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM,
                                      tools=tools, messages=messages)
        # server-side tool-loop kan pausa (pause_turn) — återuppta max 3 gånger
        conts = 0
        while getattr(resp, "stop_reason", None) == "pause_turn" and conts < 3:
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": resp.content}]
            resp = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM,
                                          tools=tools, messages=messages)
            conts += 1
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = _parse_json(text)
        data.setdefault("kalla", "webbsök")
        return data
    except Exception as e:
        _log(f"[people_finder] web search misslyckades för {bolag}: {e}")
        return {}


# ── Källa 1+2a: väg ihop hemsida + LinkedIn-träffar (Claude, ingen web tool) ────

def _pick_person(bolag: str, bransch: str, target_role: str,
                 website_text: str, linkedin_hits: list, strategy: str) -> dict:
    """Låt Claude peka ut bästa person ur hemsidetext + LinkedIn-träffar."""
    li_block = "\n".join(
        f"- {h['title']} | {h['url']} | {h.get('description','')[:160]}"
        for h in linkedin_hits[:10]
    ) or "(inga LinkedIn-träffar)"
    web_block = website_text[:3000] if website_text else "(ingen hemsidetext)"
    strat_block = (f"MINNE — tidigare lärdomar (använd bara om relevant):\n{strategy}\n\n"
                   if strategy else "")

    user_message = (
        f"Bolag: {bolag}\n"
        f"Bransch: {bransch}\n"
        f"Roll vi helst vill nå: {target_role or '(ospecificerad — välj lämpligast)'}\n\n"
        f"{strat_block}"
        f"KÄLLA 1 — text från bolagets hemsida:\n{web_block}\n\n"
        f"KÄLLA 2 — publika LinkedIn-träffar (från Google):\n{li_block}\n\n"
        f"Peka ut den bästa personen att kontakta. Returnera JSON."
    )
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
        return _parse_json(response.content[0].text)
    except Exception as e:
        _log(f"[people_finder] Claude-pick misslyckades för {bolag}: {e}")
        return {}


# ── Publikt gränssnitt ─────────────────────────────────────────────────────────

def find_person(bolag: str, website: str = "", target_role: str = "",
                bransch: str = "") -> dict:
    """
    Hitta bästa person att kontakta. Returnerar dict (se SYSTEM) — namn tomt om inget hittas.

    Väg: hemsidan (gratis) → LinkedIn via Apify om krediter finns → annars/därefter
    gratis Claude web search. Nyckeln 'method' ('apify' | 'claude_search' | 'website' |
    'none') säger vilken väg som gav svaret; ADDITIV så äldre anropare (som bara läser
    namn/titel/linkedin_url) inte påverkas.
    """
    bolag = (bolag or "").strip()
    if not bolag:
        return {"namn": "", "kalla": "ingen", "sakerhet": "låg", "method": "none"}

    # Open Brain: hämta ev. strategi för branschen innan vi söker.
    strategy = _recall_strategy(bransch, bolag)

    roles = ([target_role] if target_role else []) + DEFAULT_ROLES

    # Källa 1 — hemsidan (gratis, alltid)
    website_text = ""
    if website:
        try:
            website_text = apify.fetch_people_pages(website)
        except Exception:
            website_text = ""

    use_apify = apify.is_configured() and _apify_credits_ok()

    data: dict = {}
    method = "none"
    linkedin_hits: list = []

    # Källa 2a — Google → LinkedIn via Apify (bara om konfigurerat + krediter kvar)
    if use_apify:
        try:
            linkedin_hits = apify.find_linkedin_profiles(bolag, roles, max_results=10)
        except Exception:
            linkedin_hits = []
        if website_text or linkedin_hits:
            data = _pick_person(bolag, bransch, target_role, website_text,
                                linkedin_hits, strategy)
            method = "apify" if linkedin_hits else "website"

    # Källa 2b — gratis Claude web search som fallback (ingen Apify / slut på krediter /
    # Apify-vägen gav ingen person).
    if not str(data.get("namn", "")).strip():
        ws = _web_search_person(bolag, target_role, bransch, website_text, strategy)
        if str(ws.get("namn", "")).strip():
            data, method = ws, "claude_search"
        elif not data and website_text:
            # Sista utväg: pick enbart på hemsidetext (om web search också gav tomt).
            data = _pick_person(bolag, bransch, target_role, website_text, [], strategy)
            method = "website"

    namn = str(data.get("namn", "")).strip()

    # Skyddsnät: en LinkedIn-URL från Apify-vägen måste finnas bland träffarna.
    url = str(data.get("linkedin_url", "")).strip()
    if method == "apify" and url and url.lower() not in {h["url"].lower() for h in linkedin_hits}:
        url = ""

    if not namn:
        _log(f"[people_finder] {bolag}: metod={method} → ingen person")
        _remember_outcome(bolag, bransch, method, "")
        return {"namn": "", "kalla": "ingen", "sakerhet": "låg", "method": method,
                "motivering": str(data.get("motivering", "")).strip()
                or "Ingen publik källa hittade en person.",
                "kandidater": linkedin_hits[:5]}

    # Konstruera personlig e-post om vi vet vem personen är + har hemsida.
    email_info: dict = {}
    if namn and website:
        try:
            existing = apify._EMAIL_RE.findall(website_text)
            email_info = apify.construct_person_email(namn, website, existing)
        except Exception:
            email_info = {}

    titel = str(data.get("titel", "")).strip() or target_role
    _log(f"[people_finder] {bolag}: metod={method} → {namn} ({titel})")
    _remember_outcome(bolag, bransch, method, namn, titel)

    _default_kalla = {"apify": "linkedin", "claude_search": "webbsök"}.get(method, "hemsida")
    return {
        "namn": namn,
        "titel": titel,
        "linkedin_url": url,
        "kalla": str(data.get("kalla", "")).strip() or _default_kalla,
        "sakerhet": str(data.get("sakerhet", "")).strip() or "låg",
        "motivering": str(data.get("motivering", "")).strip(),
        "kandidater": linkedin_hits[:5],
        # Vilken väg som gav svaret (additiv nyckel).
        "method": method,
        # E-post konstruerad från namnmönster + domän
        "email": email_info.get("email", ""),
        "email_candidates": email_info.get("candidates", []),
        "email_pattern": email_info.get("pattern", ""),
        "email_verified": email_info.get("verified"),
        "email_catch_all": email_info.get("catch_all", False),
    }
