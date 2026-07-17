"""
Gemensamma hjälpare och konstanter som sidorna delar på.

Allt som förr låg överst i app.py och användes av flera sidor bor här nu, så
att det finns på EN plats. Ändrar du t.ex. statuslistan gör du det här.
"""

import urllib.parse

import streamlit as st

from database import supabase_client as db


# ── Konstanter (förr inskrivna på flera ställen) ─────────────────────────────

# Alla statusar en kontakt kan ha, i pipeline-ordning. Används av statusfilter,
# statusväljare och redigering i Översikt.
PIPELINE_STATUSES = [
    "ej_kontaktad", "skickad", "followup_1", "followup_2",
    "svar_ja", "svar_nej", "inget_svar", "mote_bokat", "avbojd",
]

# Kategori på en kontakt — så David kan urskilja vem som är vad utan att minnas
# allt. Används av manuell kontaktskapning, redigering och filter i Översikt.
KONTAKT_KATEGORIER = [
    "Prospekt", "Kund", "Partner", "Återförsäljare", "Leverantör", "Övrig",
]
KATEGORI_BADGE = {
    "Prospekt": "🌱", "Kund": "💰", "Partner": "🤝",
    "Återförsäljare": "🏪", "Leverantör": "📦", "Övrig": "•",
}


def kategori_label(kategori: str | None) -> str:
    """'Kund' → '💰 Kund'. Tom kategori → ''."""
    k = (kategori or "").strip()
    if not k:
        return ""
    return f"{KATEGORI_BADGE.get(k, '•')} {k}"

# Etiketter för mejlskrivarens rollspår och confidence.
ROLL_LABEL = {"vd": "VD/Ägare", "cfo": "CFO/Ekonomichef",
              "scm": "Inköp/Supply Chain", "neutral": "Neutral (CFO-lutad)"}
CONF_LABEL = {"high": "✅ Hög", "medium": "🟡 Medium", "low": "🔴 Låg — granska!"}


# ── Cachade läsningar ────────────────────────────────────────────────────────
# Streamlit kör om hela skriptet vid varje klick/sidbyte. Utan cache hämtas allt
# från molndatabasen på nytt varje gång → seg känsla. Vi cachar de återkommande
# läsningarna en kort stund och tömmer cachen direkt efter en skrivning, så du
# aldrig ser inaktuell data efter en åtgärd.

_CACHE_TTL = 45  # sekunder


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def cached_prospects(status=None, min_score: int = 0):
    return db.get_prospects(status=status, min_score=min_score)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def cached_pipeline_stats():
    return db.get_pipeline_stats()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def cached_sent_emails(limit: int = 100):
    return db.get_sent_emails(limit=limit)


def clear_data_cache() -> None:
    """Töm cachade läsningar — anropas efter varje skrivning så vyn blir färsk."""
    cached_prospects.clear()
    cached_pipeline_stats.clear()
    cached_sent_emails.clear()


# ── Navigering ───────────────────────────────────────────────────────────────

def goto(target: str) -> None:
    """Callback för navigeringsknappar — byter sida i menyn."""
    st.session_state["nav"] = target


# ── LinkedIn-länkar ──────────────────────────────────────────────────────────

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


# ── DM & mejl ────────────────────────────────────────────────────────────────

def generate_best_dm(p: dict, best_variant: str = "variant_b") -> str:
    """Generera ETT DM (bästa vinkeln) för en kontakt, med ev. hemsidekontext."""
    from agents.dm_generator import generate_dm_variants

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
    Visar mejl-komponenten med rollanpassad skrivning (v2).
    Returnerar (to, subject, body, send_clicked).
    """
    from integrations import email_sender, apify_research as _apify
    from agents import email_writer

    if not email_sender.is_configured():
        st.warning("Koppla SMTP — lägg `SMTP_USER` + `SMTP_PASS` i `.env` och starta om appen.")
        return None, None, None, False

    opts = to_options or []
    if to_default and to_default not in opts:
        opts = [to_default] + opts
    opts = list(dict.fromkeys(o for o in opts if o))

    # Prefill "Till" robust: seeda session_state med den sparade/kända adressen
    # och läk tomt eller ogiltigt värde. (Bara `value=` räcker inte — Streamlit
    # ignorerar det så fort widget-nyckeln finns i session_state, vilket gör att
    # fältet annars fastnar tomt efter första interaktionen och man måste skriva
    # in adressen igen.)
    to_key = f"to_{uid}"
    default_addr = opts[0] if opts else (to_default or "")

    if len(opts) > 1:
        namn = draft_kwargs.get("namn", "")
        if st.session_state.get(to_key) not in opts:
            st.session_state[to_key] = default_addr
        st.selectbox("Till", opts, format_func=lambda e: f"{namn} — {e}" if namn else e,
                     key=to_key)
    else:
        if not st.session_state.get(to_key) and default_addr:
            st.session_state[to_key] = default_addr
        st.text_input("Till", key=to_key)

    # Nyhetsresearch-toggle (visas bara om Apify är konfigurerat)
    use_research = False
    if _apify.is_configured():
        use_research = st.checkbox(
            "🔍 Sök bolagsnyheter innan utkast (Apify, ~10 sek extra)",
            key=f"research_{uid}",
            help="Söker Google efter nyheter om bolaget — ger mer personaliserat öppning i mejlet.",
        )

    if st.button("✍️ Skriv utkast", key=f"draft_{uid}"):
        with st.spinner("Skriver rollanpassat mejl..."):
            try:
                nyheter = ""
                if use_research:
                    with st.spinner("Söker bolagsnyheter..."):
                        nyheter = email_writer.fetch_company_context(
                            draft_kwargs.get("bolag", ""),
                            draft_kwargs.get("bransch", ""),
                        )
                d = email_writer.generate_email(**draft_kwargs, nyheter=nyheter)
                st.session_state[f"subj_{uid}"] = d["subject"]
                st.session_state[f"body_{uid}"] = d["body"]
                st.session_state[f"roll_{uid}"] = d.get("roll_spår", "neutral")
                st.session_state[f"conf_{uid}"] = d.get("confidence", "medium")
                st.session_state[f"review_{uid}"] = d.get("review_flag", False)
                st.session_state[f"draftdone_{uid}"] = True
            except Exception as e:
                if "overloaded" in str(e).lower() or "529" in str(e):
                    st.error("Anthropics servrar är tillfälligt överbelastade — "
                             "vänta en minut och klicka på 'Skriv utkast' igen.")
                else:
                    st.error(f"Kunde inte skriva utkast: {e}")

    if st.session_state.get(f"draftdone_{uid}"):
        # Metainfo om utkastet
        roll = st.session_state.get(f"roll_{uid}", "neutral")
        conf = st.session_state.get(f"conf_{uid}", "medium")
        review = st.session_state.get(f"review_{uid}", False)
        mc1, mc2 = st.columns(2)
        mc1.caption(f"Rollspår: **{ROLL_LABEL.get(roll, roll)}**")
        mc2.caption(f"Confidence: **{CONF_LABEL.get(conf, conf)}**")
        if review:
            st.warning("⚠️ Låg confidence — kontrollera mailet noggrant innan du skickar. "
                       "Saknar bolagsspecifika siffror eller trigger.")

        st.text_input("Ämne", key=f"subj_{uid}")
        st.text_area("Meddelande", key=f"body_{uid}", height=240)

        if review:
            send = st.button("📨 Skicka mejl (granskat)", key=f"sendmail_{uid}",
                             type="primary", help="Du har granskat mailet och godkänner det.")
        else:
            send = st.button("📨 Skicka mejl", key=f"sendmail_{uid}", type="primary")

        return (st.session_state.get(f"to_{uid}", to_default),
                st.session_state.get(f"subj_{uid}", ""),
                st.session_state.get(f"body_{uid}", ""), send)
    return st.session_state.get(f"to_{uid}", to_default), None, None, False


def render_company_analysis(a: dict) -> None:
    """Rendera en djup IHA-föranalys (från company_analyzer.analyze_company) snyggt."""
    kpi = a.get("kpi") or a.get("tal") or {}

    # Headline-siffra först — det som får bolaget att haja till.
    if a.get("headline"):
        st.success(f"🎯 {a['headline']}")

    # Nyckeltalskort (bara de som finns).
    if kpi:
        m1, m2, m3 = st.columns(3)
        if kpi.get("varulager_msek") is not None:
            m1.metric("Kapital i lager", f"{kpi['varulager_msek']} MSEK")
        if kpi.get("dos_dagar"):
            norm = f"{kpi.get('dos_norm_lag')}–{kpi.get('dos_norm_hog')}"
            m2.metric("Days of Stock", f"~{kpi['dos_dagar']} d",
                      delta=f"norm {norm} d ({kpi.get('dos_norm_bransch','')})",
                      delta_color="off")
        if kpi.get("frigorbart_lag_msek"):
            m3.metric("Frigörbart (est.)",
                      f"{kpi['frigorbart_lag_msek']}–{kpi['frigorbart_hog_msek']} MSEK")
        n1, n2, n3 = st.columns(3)
        if kpi.get("lageroms_hastighet"):
            n1.metric("Lagervarv/år", kpi["lageroms_hastighet"])
        if kpi.get("overlager_msek"):
            n2.metric("Överlager mot norm", f"{kpi['overlager_msek']} MSEK",
                      delta=f"~{kpi.get('overlager_dagar')} dagar", delta_color="off")
        if kpi.get("arlig_lagerkostnad_msek"):
            _extra = (f"~{kpi['lagerkostnad_andel_av_vinst_pct']}% av vinsten"
                      if kpi.get("lagerkostnad_andel_av_vinst_pct") else None)
            n3.metric("Årlig lagerkostnad", f"{kpi['arlig_lagerkostnad_msek']} MSEK",
                      delta=_extra, delta_color="off")
        # Vilken affärsmodell benchmarken vilar på (klassad vs gissad).
        if kpi.get("dos_dagar"):
            _kalla = kpi.get("affarsmodell_kalla", "")
            _mod = kpi.get("affarsmodell") or kpi.get("dos_norm_bransch", "")
            st.caption(f"📐 Benchmark: **{_mod}** ({_kalla}) — "
                       f"norm {kpi.get('dos_norm_lag')}–{kpi.get('dos_norm_hog')} dagar.")

    if a.get("sammanfattning"):
        st.markdown(f"**Sammanfattning.** {a['sammanfattning']}")
    if a.get("diagnos"):
        st.markdown(f"**Diagnos (hypotes).** {a['diagnos']}")
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
    if a.get("caveats"):
        with st.expander("🔎 Så håller vi analysen trovärdig (förbehåll)"):
            for c in a["caveats"]:
                st.caption(f"• {c}")


# ── Litet felhanterings-skal (ersätter upprepade try/except → st.error) ──────

class action:
    """
    Context manager som fångar fel och visar dem snyggt istället för att
    upprepa `try/except Exception as e: st.error(f"Fel: {e}")` överallt.

        with shared.action("Kunde inte spara"):
            db.save(...)
            st.success("Sparat!")

    Sätt rerun=True för att köra st.rerun() när blocket lyckats.
    """

    def __init__(self, felmeddelande: str = "Fel", rerun: bool = False):
        self.felmeddelande = felmeddelande
        self.rerun = rerun

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            if self.rerun:
                st.rerun()
            return False
        st.error(f"{self.felmeddelande}: {exc}")
        return True  # svälj felet — appen kraschar inte
