import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Du skriver det ALLRA FÖRSTA LinkedIn-meddelandet åt David Leifsson (Logistics Doctor).

MÅLET med första meddelandet är ENDAST att få ett svar och öppna ett samtal.
Det ska INTE sälja, INTE pitcha, INTE beskriva något problem. Säljet kommer långt senare,
efter flera vändor i samtalet. Folk är trötta på sälj-DM och svarar bara om det är
löjligt enkelt att svara — så enkelt att det är jobbigare att INTE svara.

GULDREGELN: En enda, kort, lätt fråga som går att svara "ja" eller "nej" på, på 2 sekunder.

FÖRBJUDET i första meddelandet (avslöjar att du säljer → inget svar):
- Orden: lageroptimering, döda artiklar, stockouts, kapitalbindning, frigöra kapital,
  effektivisera, optimera, lösning, tjänst, hjälpa er, spara pengar, konsult, IHA, analys
- Inga problembeskrivningar ("många brottas med...", "en vanlig utmaning är...")
- Inget "vi ser att...", ingen inställsam komplimang, ingen länk, inget erbjudande
- Ingen mening om vad David gör eller säljer

SÅ HÄR SKA DET LÅTA (nyfiken kollega, inte säljare):
- "Hej Anna, jobbar du mycket med lagerfrågor i din roll?"
- "Hej Erik, har du lager och artiklar att hålla koll på i jobbet?"
- "Hej Sara, sysslar du med lager/lagerstyrning på [bolag]?"

Format:
- Alltid svenska, alltid börja med "Hej [förnamn],"
- Helst EN mening, max två korta. Inga utropstecken, ingen säljton.
- Använd titel/bransch bara för att formulera den enkla frågan naturligt — inte för att
  ta upp problem eller insikter.

Returnera ett JSON-objekt med exakt tre nycklar — tre olika enkla formuleringar av samma
låga-tröskel-fråga:
{
  "variant_a": "rakt på sak — kortast möjliga, t.ex. 'jobbar du med lager?'",
  "variant_b": "lite mjukare/nyfiken, fortfarande bara en enkel fråga",
  "variant_c": "med en lätt naturlig koppling till deras roll/bolag, men ändå bara en enkel fråga"
}
Inga förklaringar utanför JSON-blocket."""


def _get_first_name(namn: str) -> str:
    namn = (namn or "").strip()
    return namn.split()[0] if namn else ""


def generate_dm_variants(namn: str, titel: str, bolag: str, bransch: str,
                         extra_guidance: str = "", website_context: str = "") -> dict:
    """
    Generate three DM variants for a prospect. Returns dict with variant_a/b/c.
    extra_guidance: valfri "vad funkar"-brief från inlärnings-agenten som lutar
    texten mot det som historiskt gett flest svar.
    website_context: valfri text från bolagets hemsida (research-agenten) så att den
    enkla frågan kan kännas specifik och personlig — ALDRIG för att pitcha eller
    nämna problem.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    fornamn = _get_first_name(namn)

    guidance_block = ""
    if extra_guidance.strip():
        guidance_block = (
            f"\n\nLÄRDOMAR FRÅN TIDIGARE UTSKICK (väg in detta):\n{extra_guidance.strip()}\n"
        )

    website_block = ""
    if website_context.strip():
        website_block = (
            f"\n\nOM BOLAGET (från deras hemsida — använd ENDAST för att göra den enkla "
            f"frågan naturlig/specifik, t.ex. anknyt lätt till vad de tillverkar. "
            f"ALDRIG för att nämna problem, lager eller sälj):\n{website_context.strip()[:600]}\n"
        )

    user_message = (
        f"Generera tre DM-varianter för denna kontakt:\n"
        f"- Namn: {namn} (använd förnamn: {fornamn})\n"
        f"- Titel: {titel}\n"
        f"- Bolag: {bolag}\n"
        f"- Bransch: {bransch}\n"
        f"{guidance_block}"
        f"{website_block}\n"
        f"Returnera endast JSON med nycklarna variant_a, variant_b, variant_c."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # Extract JSON even if wrapped in code fences
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    variants = json.loads(raw)
    return variants


CONVERSATION_SYSTEM = """Du fortsätter ett pågående LinkedIn-samtal åt David Leifsson (Logistics Doctor).
David hjälper tillverkare/distributörer att få ordning på lager och kapitalbindning, men
DET ska du INTE avslöja för tidigt. Samtalet vinns med tålamod, inte med pitch.

GRUNDPRINCIP: Lågt tryck. Nyfiken. Ett litet steg i taget. Spegla personens ton och längd —
svarar de kort, svara kort. Pitcha ALDRIG förrän personen tydligt öppnar dörren själv.

Läs av var i samtalet ni är och välj nästa steg:
- De har precis bekräftat att de jobbar med lager → visa äkta nyfikenhet på deras vardag.
  Ställ EN lätt, öppen fråga om hur de har det (t.ex. hur de håller koll på artiklar idag,
  om det är manuellt/i affärssystemet). Sälj inte. Nämn inte David's tjänst.
- De berättar om sin situation eller nämner en friktion → följ upp nyfiket, bekräfta att du
  känner igen det från andra, men erbjud fortfarande inget. Bygg förtroende.
- De öppnar tydligt (frågar vad du gör, vill veta mer, frågar om hjälp) → FÖRST DÅ får du
  kort och enkelt berätta vad David gör och föreslå ett kort samtal.

Format: svenska, kort (oftast 1–2 meningar), naturligt och mänskligt. Ingen säljjargong,
inga utropstecken, ingen länk om de inte bett om det.

YTTERST VIKTIGT OM SVARET: Skriv ENDAST själva meddelandet som ska skickas till personen,
ordagrant och färdigt att kopiera. INGEN förklaring, INGEN motivering, INGEN rubrik, inget
"---", ingen text om vad du tänker. Börja direkt med meddelandet (t.ex. "Hej ..." eller rakt
på). Skriv aldrig om David i tredje person."""


def generate_reply(namn: str, titel: str, bolag: str, deras_svar: str,
                   historik: str = "") -> str:
    """
    Generera nästa meddelande i ett pågående samtal — lågt tryck, ett steg i taget.
    deras_svar = personens senaste meddelande. historik = valfri text med tidigare turer.
    Returnerar en föreslagen meddelandetext.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    fornamn = _get_first_name(namn)

    context = f"Kontakt: {namn} ({fornamn}), {titel} på {bolag}.\n"
    if historik.strip():
        context += f"\nTidigare i samtalet:\n{historik.strip()}\n"
    context += (
        f"\nDeras senaste meddelande:\n\"{deras_svar.strip()}\"\n\n"
        f"Skriv Davids nästa meddelande. Ett litet steg framåt, lågt tryck, ingen pitch "
        f"om de inte tydligt öppnat dörren."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=CONVERSATION_SYSTEM,
        messages=[{"role": "user", "content": context}],
    )
    return _clean_reply(response.content[0].text)


def _clean_reply(raw: str) -> str:
    """Skyddsnät: ta bort ev. meta-resonemang så bara det färdiga meddelandet blir kvar."""
    import re as _re
    text = (raw or "").strip()
    # Om modellen lade en "---"-separator: ta det som kommer EFTER sista separatorn
    parts = _re.split(r'\n\s*-{3,}\s*\n', text)
    if len(parts) > 1:
        text = parts[-1].strip()
    # Ta bort ev. inledande etikett som "Meddelande:" / "Förslag:" / "Svar:"
    text = _re.sub(r'^(meddelande|förslag|svar|message)\s*:\s*', '', text, flags=_re.IGNORECASE).strip()
    return text


FOLLOWUP_TEMPLATES = {
    "followup_1": (
        "Hej {fornamn},\n\n"
        "Ville följa upp mitt mejl från för några dagar sedan. "
        "Är lagerhantering något du arbetar aktivt med i din roll?\n\n"
        "Mvh David"
    ),
    "followup_2": (
        "Hej {fornamn},\n\n"
        "Sista påminnelsen från min sida — om det inte är rätt tillfälle just nu "
        "är det helt okej. Annars tar jag gärna ett kort samtal, det tar inte mer än "
        "15 minuter.\n\n"
        "Mvh David"
    ),
}


def generate_followup(namn: str, typ: str) -> str:
    """Generate follow-up message. typ: 'followup_1' or 'followup_2'."""
    fornamn = _get_first_name(namn)
    template = FOLLOWUP_TEMPLATES.get(typ, FOLLOWUP_TEMPLATES["followup_1"])
    msg = template.format(fornamn=fornamn)
    # Ingen känd kontaktperson? Städa "Hej ," → "Hej,".
    if not fornamn:
        msg = msg.replace("Hej ,", "Hej,")
    return msg
