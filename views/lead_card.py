"""Ett lead-kort — allt som hör till att bearbeta och visa ETT lead.

Bruten ut ur leads.py (juli 2026): listsidan + alla kort låg tidigare i en
enda ~760-radersfil, och det var samtidigt filen som ändrades oftast i hela
appen — en riskabel kombination när något skulle justeras.

Uppdelningen: `views/leads.py` äger LISTAN (sortering, filter, bulk-
bearbetning av flera leads i rad). Den här filen äger ETT kort i taget —
allt som visas och kan göras för ett enskilt lead. `views/` scannas aldrig
av Streamlits sidautomatik (bara en riktig `pages/`-mapp gör det), så den
här filen dyker aldrig upp som en egen flik — den är bara en vanlig
Python-modul som `leads.py` importerar två funktioner från:

  - `enrich_lead(l, contact_cache)` — hämta hemsida/e-post/telefon/person
    för ETT lead (används av leads.py:s bulk-knapp och auto-läge).
  - `render_lead_card(l, contact_cache, analysis_cache, emailed_bolag)` —
    rita kortet.

Ingen logik är ändrad här jämfört med den gamla leads.py — bara flyttad
och uppdelad i mindre, namngivna delar så det går att hitta och ändra EN
sak i taget utan att läsa igenom 460 rader varje gång.
"""

import re

import streamlit as st

from agents import people_finder, company_analyzer, hiring_signals
from integrations import apify_research as _apify
from integrations import email_sender
from database import supabase_client as db
from views import shared
from views.shared import (person_link_inline, render_company_analysis,
                          render_email_composer, log_sent_email, kategori_label,
                          clear_data_cache)

# Open Brain-minnet (tålig import — kortet funkar även utan det).
try:
    from brain import open_brain as _brain
except Exception:
    _brain = None


# Generiska adresser (info@, order@, ...) sparas inte som leadets e-post —
# David vill bara ha personliga adresser i kortet. De skrapade adresserna syns
# ändå som länkar på kortet, de tar bara inte e-postfältets plats.
_GENERIC_LOCALPARTS = {
    "info", "kontakt", "contact", "order", "orders", "sales", "forsaljning",
    "försäljning", "office", "mail", "post", "hello", "hej", "support",
    "kundtjanst", "kundservice", "kundcenter", "customercare", "customerservice",
    "admin", "reception", "faktura", "invoice", "webmaster", "noreply",
    "no-reply", "ekonomi", "economy", "finance", "ir", "hr", "marknad",
    "marketing", "vaxel", "service", "butik", "shop", "export", "offert",
    "website", "whistleblower",
}


def _same_person(a: str, b: str) -> bool:
    """
    Samma person trots olika namnform? Registret skriver ofta alla förnamn
    ("Per Lennart Axelsson") medan hemsidan skriver tilltalsnamnet ("Per
    Axelsson"). Matchar när det ena namnets ord ryms i det andras — minst
    för- och efternamn gemensamma.
    """
    ta = set((a or "").lower().split())
    tb = set((b or "").lower().split())
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta <= tb or tb <= ta


def _personal_email(addr: str) -> str:
    """Adressen om den är personlig, annars tom sträng (info@, ekonomi@ m.fl.)."""
    addr = (addr or "").strip()
    if not addr or "@" not in addr:
        return ""
    local = addr.split("@")[0].lower()
    if local in _GENERIC_LOCALPARTS:
        return ""
    # bolag@bolag.se — lokaldelen är domännamnet = generisk företagsadress
    domain_base = addr.split("@")[1].split(".")[0].lower()
    if local == domain_base:
        return ""
    return addr


def enrich_lead(l, contact_cache) -> dict:
    """
    Berika ETT lead: hemsida (gratis gissning) + e-post + telefon (gratis) och rätt
    person (find_person — gratis web search först, Apify bara om krediter). Tålig:
    fel på ett steg stoppar inte de andra. Returnerar vad som hittades.
    """
    lid = l["id"]
    had_web = bool((l.get("website") or "").strip())
    web = (l.get("website") or "").strip()
    res = {"web": False, "mail": False, "tel": False, "person": False}

    if not web:
        try:
            web = _apify.guess_company_website(l.get("bolag", "")) or ""
        except Exception:
            web = ""
    if web:
        try:
            contact = _apify.find_emails(web, l.get("bolag", ""), render=False)
            email = (_personal_email(contact.get("best", ""))
                     or contact.get("guessed", ""))
            tel = contact.get("telefon", "")
            contact_cache[lid] = {**contact, "website": web}
            db.update_lead_suggestion_contact(lid, email=email, website=web, telefon=tel)
            res["web"] = not had_web
            res["mail"] = bool(contact.get("best"))
            res["tel"] = bool(tel)
        except Exception:
            pass
    behov_person = not (l.get("namn") or "").strip()
    behov_email = not (l.get("email") or "").strip()
    if behov_person or behov_email:
        try:
            found = people_finder.find_person(
                l.get("bolag", ""), web, l.get("titel", ""), l.get("bransch", ""))
            if behov_person and found.get("namn"):
                # Leadet saknade person → spara bästa valet + kontaktuppgifter.
                db.update_lead_suggestion_person(
                    lid, found["namn"], found.get("titel", ""),
                    found.get("linkedin_url", ""))
                res["person"] = True
                # Personlig mejl/telefon lästa vid namnet på sidan slår generiska
                # info@-adresser; hemsida upptäckt via sökningen sparas också.
                if (found.get("email") or found.get("telefon")
                        or found.get("website")):
                    db.update_lead_suggestion_contact(
                        lid, email=found.get("email", ""),
                        website=found.get("website") or web,
                        telefon=found.get("telefon", ""))
                    res["mail"] = res["mail"] or bool(found.get("email"))
                    res["tel"] = res["tel"] or bool(found.get("telefon"))
            elif behov_email:
                # Leadet HAR en person (Davids egen eller tidigare hittad) men
                # saknar mejl. Skriv ALDRIG över namnet — leta i stället upp
                # samma person bland de lästa kandidaterna och ta hens uppgifter.
                mitt_namn = (l.get("namn") or "").strip()
                for k in found.get("kandidater") or []:
                    if (_same_person(k.get("namn", ""), mitt_namn)
                            and (k.get("email") or k.get("telefon"))):
                        db.update_lead_suggestion_contact(
                            lid, email=k.get("email", ""),
                            website=found.get("website") or web,
                            telefon=k.get("telefon", ""))
                        res["mail"] = res["mail"] or bool(k.get("email"))
                        res["tel"] = res["tel"] or bool(k.get("telefon"))
                        break
        except Exception:
            pass
    return res


def render_lead_card(l, contact_cache, analysis_cache, emailed_bolag):
    """Ett lead-kort: person/e-post/godkänn + manuell kontakt, analys och mejl."""
    lid = l.get("id")
    cached = contact_cache.get(lid, {})
    website = cached.get("website") or l.get("website") or ""
    emails = cached.get("emails") or ([l["email"]] if l.get("email") else [])
    guessed = cached.get("guessed") or ""
    telefon = cached.get("telefon") or l.get("telefon") or ""

    with st.container(border=True):
        # Bredderna är INTE lika: "🔍 Kontaktuppgifter" är dubbelt så lång som
        # "❌ Avböj" och bröts mitt i ordet ("Kontaktuppgifte r") när alla fyra
        # knappkolumner var lika breda. Varje kolumn får nu plats efter sin
        # etikett.
        cols = st.columns([3, 1.6, 1.1, 1.1, 1])
        with cols[0]:
            _render_identity(l, website)
            _render_emails(l, lid, emails, guessed)
            _render_phone_and_flags(l, website, telefon)
            _render_pending_email_choice(lid, website)
            _render_pending_person_choice(l, lid, website)
        with cols[1]:
            _render_find_contact_button(l, lid)
        with cols[2]:
            _render_find_email_button(l, lid, contact_cache)
        with cols[3]:
            _render_approve_button(l, lid)
        with cols[4]:
            _render_reject_button(lid)

        # Mejladresser: manuellt sparad + skrapad + gissad. Behövs för mejlfliken.
        manual_email = l.get("email", "")
        all_emails = list(dict.fromkeys(
            e for e in ([manual_email] + emails + ([guessed] if guessed else []))
            if e
        ))
        sent_date = emailed_bolag.get((l.get("bolag") or "").lower())

        # Mejlstatus som synlig bricka på kortet — du ser den utan att öppna panelen.
        if sent_date:
            st.success(f"✅ Mejl skickat {sent_date}")

        # Rekryteringssignal som synlig bricka — den viktigaste öppningen ska
        # synas direkt, inte gömmas i "Mer"-panelen där den lätt missas.
        signals = st.session_state.get(f"signals_{lid}")
        if signals and signals.get("hittat"):
            roller = ", ".join(signals["roller_matchade"]) or "lager-/inköpsroll"
            st.success(f"🎯 Rekryterar: {roller} — sannolik köpsignal!")

        _render_paste_contact(l, lid, website)
        _render_more_panel(l, lid, website, all_emails, sent_date, analysis_cache)


# ── Vänsterkolumnen: identitet, e-post, telefon, väntande val ───────────────

def _render_identity(l, website: str) -> None:
    """Bolag/titel/bransch, personens namn + LinkedIn-länk, hemsidelänk."""
    _kb = kategori_label(l.get("kategori"))
    st.markdown(f"{(_kb + ' · ') if _kb else ''}"
                f"**{l.get('bolag')}** — {l.get('titel')} · "
                f"_{l.get('bransch','')}_ (score {l.get('score', 0)})")
    if l.get("namn"):
        _t = (l.get("titel") or "").strip()
        st.markdown(f"👤 **{l['namn']}**" + (f" — {_t}" if _t else "") + " · "
                    + person_link_inline(l["namn"], l.get("bolag", ""),
                                         l.get("linkedin_url", "")))
    else:
        st.caption("👤 _Ingen person hittad ännu — tryck 'Hitta person'._")
    if website:
        st.markdown(f"🌐 [Företagets hemsida]({website})")


def _render_emails(l, lid, emails: list, guessed: str) -> None:
    """
    Visa bara PERSONLIGA adresser — info@/order@ m.fl. är brus för David.
    Märk VARJE adress med vems den är, om vi kan avgöra det:
     1. Exakt träff mot en tidigare hittad kandidat (namn+titel+mejl
        från en personsökning) — säkrast källa.
     2. Exakt träff mot leadets SPARADE person/mejl.
     3. Namnfragment i adressens lokaldel matchar sparade personens
        namn (t.ex. "fredric@..." mot "Per Fredric Hakfelt") — en
        skrapad adresslista har annars ingen som helst namnkoppling.
    """
    _personal = [e for e in emails if _personal_email(e)]
    _saved = (l.get("email") or "").strip().lower()
    _t = (l.get("titel") or "").strip()
    _namn = (l.get("namn") or "").strip()
    _namndelar = [p.lower() for p in _namn.split() if len(p) > 2]

    _titel_per_mejl = {
        (k.get("email") or "").strip().lower(): (k.get("titel") or "").strip()
        for k in st.session_state.get(f"found_people_{lid}", [])
        if k.get("email")
    }
    if _t:
        _titel_per_mejl.setdefault(_saved, _t)

    def _email_label(e: str) -> str:
        key = e.strip().lower()
        titel = _titel_per_mejl.get(key, "")
        if titel:
            return f"[{e}](mailto:{e}) _({titel})_"
        local = key.split("@")[0].replace(".", " ").replace("-", " ")
        if _t and any(part in local for part in _namndelar):
            return f"[{e}](mailto:{e}) _({_t}?)_"
        return f"[{e}](mailto:{e})"

    if _personal:
        links = " · ".join(_email_label(e) for e in _personal[:4])
        st.markdown(f"✉️ {links}")
        st.code(_personal[0], language=None)
    elif guessed:
        st.markdown(f"✉️ {guessed}  ·  _kvalificerad gissning (ej verifierad)_")
        st.code(guessed, language=None)
    _hidden = len(emails) - len(_personal)
    if _hidden > 0:
        st.caption(f"_{_hidden} generisk(a) adress(er) dolda (info@ m.fl.) — "
                   f"finns kvar på hemsidan om de behövs._")


def _render_phone_and_flags(l, website: str, telefon: str) -> None:
    """Telefonlänk + varningar när kontaktuppgifter saknas + motivering."""
    if telefon:
        _tel_href = "tel:" + re.sub(r"[^\d+]", "", telefon)
        st.markdown(f"📞 [{telefon}]({_tel_href})")
    # Anmärkning: bearbetat lead (hemsida finns) utan personlig mejl —
    # bara generiska adresser (info@/ekonomi@) fanns, och de sparas inte.
    # Tydlig flagga så David enkelt kan avböja leadet.
    if website and not (l.get("email") or "").strip():
        if (l.get("namn") or "").strip():
            st.warning("⚠️ Ingen personlig mejl sparad — generiska adresser "
                       "(info@/ekonomi@) sparas inte. Kör 🔍 Kontaktuppgifter, "
                       "eller ❌ Avböj leadet.")
        else:
            st.warning("⚠️ Varken person eller personlig mejl — "
                       "kör 🔍 Kontaktuppgifter eller ❌ Avböj.")
    if l.get("motivering"):
        st.caption(l["motivering"])


def _render_pending_email_choice(lid, website: str) -> None:
    """E-postkandidater från senaste personsökning — väntar på val."""
    cand_key = f"found_emails_{lid}"
    if cand_key not in st.session_state:
        return
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


def _render_pending_person_choice(l, lid, website: str) -> None:
    """
    Personkandidater från senaste sökningen — välj vem som sparas på kortet.
    Titel + mejl/telefon visas bakom namnet så du ser vem som är vem.
    """
    people_key = f"found_people_{lid}"
    if people_key not in st.session_state:
        return
    folk = st.session_state[people_key]

    def _person_label(p: dict) -> str:
        label = p.get("namn", "")
        if p.get("titel"):
            label += f" — {p['titel']}"
        extra = " · ".join(x for x in (p.get("email"), p.get("telefon")) if x)
        return label + (f"  ({extra})" if extra else "")

    st.info(f"👥 {len(folk)} personer hittades på hemsidan — välj vem som sparas:")
    psel = st.selectbox("Person", folk, format_func=_person_label,
                        key=f"sel_person_{lid}",
                        label_visibility="collapsed")
    pc1, pc2 = st.columns([2, 1])
    with pc1:
        if st.button("💾 Spara vald person", key=f"save_person_{lid}",
                     type="primary"):
            db.update_lead_suggestion_person(
                lid, psel["namn"], psel.get("titel", ""),
                l.get("linkedin_url", ""))
            if psel.get("email") or psel.get("telefon"):
                db.update_lead_suggestion_contact(
                    lid, email=psel.get("email", ""), website=website,
                    telefon=psel.get("telefon", ""))
            del st.session_state[people_key]
            st.rerun()
    with pc2:
        if st.button("Stäng", key=f"close_people_{lid}",
                     help="Behåll nuvarande person och göm listan."):
            del st.session_state[people_key]
            st.rerun()


# ── Knappkolumnerna ───────────────────────────────────────────────────────

def _render_find_contact_button(l, lid) -> None:
    """🔍 Kontaktuppgifter — läser hemsidan efter rätt person."""
    if not (lid and st.button("🔍 Kontaktuppgifter", key=f"person_{lid}",
                              width="stretch")):
        return
    with st.spinner("Läser bolagets hemsida efter rätt person..."):
        with shared.action("Kunde inte hämta kontaktuppgifter"):
            found = people_finder.find_person(
                l.get("bolag", ""), l.get("website", ""),
                l.get("titel", ""), l.get("bransch", ""))
            if found.get("namn"):
                db.update_lead_suggestion_person(
                    l["id"], found["namn"],
                    found.get("titel", ""), found.get("linkedin_url", ""))
                msg = f"{found['namn']} ({found.get('sakerhet','?')} säkerhet)"
                # Personlig mejl/telefon lästa vid namnet på sidan → spara
                # direkt. Hemsida som sökningen upptäckte (leadet saknade
                # en, t.ex. Meson AB → mesongroup.com) sparas också.
                if (found.get("email") or found.get("telefon")
                        or found.get("website")):
                    db.update_lead_suggestion_contact(
                        lid, email=found.get("email", ""),
                        website=found.get("website") or l.get("website", ""),
                        telefon=found.get("telefon", ""))
                    _extra = " · ".join(x for x in (found.get("email"),
                                                    found.get("telefon")) if x)
                    msg += f" — {_extra}"
                # Spara e-postkandidater i session state → visas som selectbox i kortet
                if found.get("email_candidates"):
                    st.session_state[f"found_emails_{lid}"] = found["email_candidates"]
                    st.session_state[f"found_pat_{lid}"] = found.get("email_pattern", "")
                    msg += " — välj e-post nedan"
                # Fler personer lästa på sidan → visa väljare i kortet
                if len(found.get("kandidater") or []) > 1:
                    st.session_state[f"found_people_{lid}"] = found["kandidater"]
                    msg += f" — {len(found['kandidater'])} personer hittades, välj nedan"
                st.success(msg)
                st.rerun()
            else:
                # Visa VARFÖR det blev tomt + vilken sida som lästes —
                # gör felsökning möjlig utan att gräva i serverloggen.
                _why = (found.get("motivering") or "").strip()
                _src = (found.get("källa") or "").strip()
                st.warning("Hittade ingen tydlig person."
                           + (f" {_why}" if _why else "")
                           + (f" Läste: {_src}" if _src
                              else " Ingen sida gick att läsa — kan vara "
                                   "JS-sajt eller blockerad hämtning."))


def _render_find_email_button(l, lid, contact_cache) -> None:
    """✉️ E-post — letar e-post på hemsidan (renderar JS vid behov)."""
    if not (lid and st.button("✉️ E-post", key=f"email_{lid}",
                              width="stretch")):
        return
    with st.spinner("Letar e-post på hemsidan (renderar JS vid behov)..."):
        with shared.action("Kunde inte söka e-post"):
            # find_emails anropar bara Apify som sista utväg om den gratis
            # sökningen gav noll adresser — de flesta gånger nollställs det
            # här utan att något nytt fel någonsin sätts.
            _apify.clear_last_error()
            contact = _apify.find_emails(l.get("website", ""),
                                         l.get("bolag", ""), render=True)
            contact_cache[lid] = contact
            db.update_lead_suggestion_contact(
                lid,
                email=(_personal_email(contact.get("best", ""))
                       or contact.get("guessed", "")),
                website=contact.get("website", ""), telefon=contact.get("telefon", ""))
            _tel = contact.get("telefon", "")
            if contact.get("best"):
                via = " (via renderad sida)" if contact.get("rendered") else ""
                _telmsg = f" · 📞 {_tel}" if _tel else ""
                st.success(f"Hittade {len(contact['emails'])} adress(er){via}{_telmsg}.")
            elif contact.get("guessed"):
                st.info(f"Ingen publik adress — gissar {contact['guessed']} "
                        "(verifiera innan du mejlar).")
            elif contact.get("website"):
                st.warning("Hittade hemsidan men ingen publik e-post.")
            else:
                st.warning("Hittade ingen hemsida/e-post.")
            _apify_err = _apify.get_last_error()
            if _apify_err:
                st.warning(f"⚠️ Apify: {_apify_err}")
            st.rerun()


def _render_approve_button(l, lid) -> None:
    """✅ Godkänn — flyttar leadet till pipeline."""
    if not (lid and st.button("✅ Godkänn", key=f"approve_{lid}",
                              type="primary", width="stretch")):
        return
    with shared.action("Kunde inte godkänna leadet"):
        db.promote_lead(l)
        clear_data_cache()
        st.success("Tillagd i pipeline!")
        st.rerun()


def _render_reject_button(lid) -> None:
    """❌ Avböj — tar bort leadet ur listan."""
    if not (lid and st.button("❌ Avböj", key=f"reject_{lid}",
                              width="stretch",
                              help="Passar inte (fel bransch/storlek e.d.) — "
                                   "tas bort ur listan.")):
        return
    with shared.action("Kunde inte avböja leadet"):
        db.update_lead_suggestion(lid, "rejected")
        st.success("Avböjd — borttagen ur leads.")
        st.rerun()


# ── Nedre panelerna: klistra in kontakt + "Mer"-expandern ───────────────────

def _render_paste_contact(l, lid, website: str) -> None:
    """Snabb-genväg: klistra in kontakt (för hårda bolag skraparen missar)."""
    with st.expander("📋 Klistra in kontakt"):
        raw = st.text_area(
            "Klistra in namn / e-post / telefon (valfritt format)",
            key=f"paste_{lid}", height=90,
            placeholder="Tony Ekström, VD\ntony@soliferpolar.com\n0942-520 00")
        if not st.button("📋 Tolka & spara", key=f"paste_save_{lid}", type="primary"):
            return
        txt = raw or ""
        _mails = _apify._EMAIL_RE.findall(txt)
        _phones = _apify._extract_phones(txt)
        p_email = _mails[0].strip().lower() if _mails else ""
        p_tel = _phones[0] if _phones else ""
        # Namn: en manuell inklistring är alltid avsiktlig, så den vinner
        # även om leadet redan har ett (t.ex. registrets VD-namn) — annars
        # sparas David:s korrekta mejl/telefon men det gamla, fel namnet
        # ligger kvar. Ta första rad utan @ och utan långt sifferblock
        # (2–5 ord), som ser ut som ett namn.
        p_namn = ""
        for line in txt.splitlines():
            # Dra bort ev. roll efter komma: "Tony Ekström, VD" → "Tony Ekström"
            cand = line.strip().split(",")[0].strip()
            if (cand and "@" not in cand and not re.search(r"\d{4,}", cand)
                    and 2 <= len(cand.split()) <= 4):
                p_namn = cand
                break
        if not (p_email or p_tel or p_namn):
            st.warning("Hittade varken namn, e-post eller telefon i texten.")
            return
        with shared.action("Kunde inte spara inklistrad kontakt"):
            _old_n = (l.get("namn") or "").strip()
            if p_namn and _brain and _brain.is_configured():
                try:
                    if _old_n and _old_n != p_namn:
                        _thought = (
                            f"[people_finder-lärdom] {l.get('bolag','')} "
                            f"({l.get('bransch','')}): agenten hade sparat "
                            f"\"{_old_n}\" (troligen bolagsregistrets VD), men "
                            f"David rättade till \"{p_namn}\" efter manuell "
                            "koll på hemsidan — leta djupare på Kontakt/"
                            "Ledning-sidor, lita inte på registrets VD-namn.")
                    else:
                        _thought = (
                            f"[people_finder-lärdom] {l.get('bolag','')} "
                            f"({l.get('bransch','')}): agenten hade ingen person, "
                            f"David klistrade in \"{p_namn}\" — leta djupare på "
                            "Om oss/Ledning/Kontakt för liknande bolag.")
                    _brain.capture_thought(_thought[:400])
                except Exception:
                    pass
            if p_namn:
                db.update_lead_suggestion_person(
                    lid, p_namn, l.get("titel", ""), l.get("linkedin_url", ""))
            if p_email or p_tel or website:
                db.update_lead_suggestion_contact(
                    lid, email=p_email, website=website, telefon=p_tel)
            st.success(f"Sparat — namn: {p_namn or '(oförändrat)'} · "
                       f"e-post: {p_email or '—'} · telefon: {p_tel or '—'}")
            st.rerun()


def _render_more_panel(l, lid, website: str, all_emails: list,
                       sent_date: str, analysis_cache: dict) -> None:
    """Sekundära åtgärder samlade under EN panel (fyra flikar) så listan blir
    lätt att skanna. Öppna bara det kort du jobbar med."""
    with st.expander("➕ Mer — kontakt, IHA-analys, signaler & mejl"):
        tab_kontakt, tab_analys, tab_signaler, tab_mejl = st.tabs(
            ["✏️ Kontakt", "📊 IHA-analys", "🎯 Signaler", "📧 Mejl"])
        with tab_kontakt:
            _render_contact_tab(l, lid, website)
        with tab_analys:
            _render_analysis_tab(l, lid, website, analysis_cache)
        with tab_signaler:
            _render_signals_tab(l, lid)
        with tab_mejl:
            _render_email_tab(l, lid, website, all_emails, sent_date, analysis_cache)


def _render_signals_tab(l, lid) -> None:
    """
    Rekryteringssignal: söker jobbannonser för inköps-/lager-/logistikroller
    hos bolaget via Arbetsförmedlingens Platsbank (se agents/hiring_signals.py
    för resonemanget). Helt gratis — inget Apify, ingen nyckel, ingen kostnad.
    Körs bara på knapptryck, aldrig automatiskt.
    """
    cached = st.session_state.get(f"signals_{lid}")
    label = "🎯 Kolla jobbannonser" if not cached else "🔄 Sök igen"
    if st.button(label, key=f"signals_btn_{lid}"):
        with st.spinner("Söker jobbannonser i Platsbanken (inköp/lager/logistik)..."):
            try:
                result = hiring_signals.find_hiring_signals(l.get("bolag", ""))
                st.session_state[f"signals_{lid}"] = result
                st.rerun()
            except Exception as e:
                st.error(f"Fel: {e}")
                return
    if not cached:
        st.caption("Tryck **Kolla jobbannonser** — söker Arbetsförmedlingens "
                   "Platsbank efter lediga inköps-/lager-/logistiktjänster hos "
                   "bolaget just nu. En stark köpsignal: antingen saknar de "
                   "kompetensen, eller växer och moderniserar. Helt gratis, "
                   "ingen Apify-kredit dras.")
        return
    if cached["hittat"]:
        roller = ", ".join(cached["roller_matchade"]) or "lager-/inköpsroll"
        st.success(f"🎯 Rekryterar: {roller} — sannolik köpsignal!")
        for t in cached["traffar"]:
            titel = t.get("title") or t.get("url", "")
            st.markdown(f"- [{titel}]({t.get('url', '')})")
            if t.get("description"):
                st.caption(t["description"][:160])
    else:
        st.caption("Inga jobbannonser för inköp/lager/logistik hittade just nu.")


def _render_contact_tab(l, lid, website: str) -> None:
    """Manuell kontakt (när automatik inte hittar rätt person)."""
    with st.form(key=f"manual_{lid}"):
        # OBS: .get(key, "") ger None om kolumnen finns men är null →
        # text_input returnerar då None och .strip() kraschar. Tvinga str.
        m_namn  = st.text_input("Namn", value=l.get("namn") or "",
                                placeholder="Anna Lindqvist")
        m_titel = st.text_input("Roll", value=l.get("titel") or "",
                                placeholder="Inköpschef")
        m_li    = st.text_input("LinkedIn-URL (valfritt)",
                                value=l.get("linkedin_url") or "",
                                placeholder="https://linkedin.com/in/...")
        c1, c2 = st.columns(2)
        with c1:
            m_email = st.text_input("E-post (valfritt)",
                                    value=l.get("email") or "",
                                    placeholder="anna.lindqvist@foretag.se")
        with c2:
            m_tel = st.text_input("Telefon (valfritt)",
                                  value=l.get("telefon") or "",
                                  placeholder="+46 70 123 45 67")
        if not st.form_submit_button("💾 Spara"):
            return
        with shared.action("Kunde inte spara kontaktuppgifterna"):
            # Lär agenten via Open Brain. TVÅ fall:
            #  1. Rättning: David skriver över en felgissning.
            #  2. Lärdom: agenten hittade INGEN, David hittade personen
            #     själv (t.ex. via hemsidan) — den mest värdefulla signalen.
            # Sparas → återanvänds av find_person för liknande bolag.
            _old = (l.get("namn") or "").strip()
            _new = (m_namn or "").strip()
            _rol = (m_titel or "").strip()
            if _new and _new != _old and _brain and _brain.is_configured():
                if _old:
                    _note = (f"[people_finder-rättning] {l.get('bolag','')} "
                             f"({l.get('bransch','')}): agenten gissade "
                             f"\"{_old}\" men rätt person är \"{_new}\""
                             + (f", {_rol}" if _rol else "")
                             + ". Vikta den rollen/källan högre för liknande bolag.")
                else:
                    _note = (f"[people_finder-lärdom] {l.get('bolag','')} "
                             f"({l.get('bransch','')}): agenten hittade ingen "
                             f"person, men rätt kontakt är \"{_new}\""
                             + (f", {_rol}" if _rol else "")
                             + " — hittad manuellt på hemsidan. Leta djupare på "
                             "Om oss/Ledning/Kontakt-sidor för liknande bolag.")
                try:
                    _brain.capture_thought(_note[:400])
                except Exception:
                    pass
            _li = (m_li or "").strip()
            _email = (m_email or "").strip()
            _tel = (m_tel or "").strip()
            if _new:
                db.update_lead_suggestion_person(
                    lid, _new, _rol, _li)
            if _email or _tel or website:
                db.update_lead_suggestion_contact(
                    lid, email=_email,
                    website=website, telefon=_tel)
            st.success("Sparat!")
            st.rerun()


def _render_analysis_tab(l, lid, website: str, analysis_cache: dict) -> None:
    """IHA-föranalys (siffror + hemsida) innan kontakt."""
    cached_a = analysis_cache.get(lid)

    def _run(model_override=""):
        with st.spinner("Analyserar bolagets lagerläge (siffror + hemsida)..."):
            with shared.action("Kunde inte analysera bolaget"):
                analysis_cache[lid] = company_analyzer.analyze_company(
                    bolag=l.get("bolag", ""), bransch=l.get("bransch", ""),
                    website=website, omsattning_msek=l.get("omsattning"),
                    varulager_msek=l.get("varulager"),
                    resultat_msek=l.get("resultat"),
                    anstallda=l.get("anstallda"),
                    lagerandel=l.get("lagerandel"),
                    vinstmarginal=l.get("vinstmarginal"),
                    orgnr=l.get("orgnr", ""), affarsmodell=model_override)
                st.rerun()

    if st.button("🔬 Gör analys" if not cached_a else "🔄 Gör om analys",
                 key=f"analyze_{lid}"):
        _run()
    if cached_a:
        render_company_analysis(cached_a)
        # Överstyr affärsmodellen om klassningen blev fel (du är experten).
        _MODELS = ["tillverkning", "grossist", "handel", "bygg"]
        _cur = cached_a.get("affarsmodell", "")
        with st.expander("📐 Justera affärsmodell (styr benchmark)"):
            oc1, oc2 = st.columns([2, 1])
            pick = oc1.selectbox(
                "Affärsmodell", _MODELS,
                index=_MODELS.index(_cur) if _cur in _MODELS else 0,
                key=f"model_{lid}")
            if oc2.button("Räkna om", key=f"remodel_{lid}"):
                _run(model_override=pick)
    else:
        st.caption("Tryck **Gör analys** — väver ihop bolagets bokslutssiffror "
                   "med deras hemsida till en säljbar bild (drar ett API-anrop).")


def _render_email_tab(l, lid, website: str, all_emails: list,
                      sent_date: str, analysis_cache: dict) -> None:
    """Mejla direkt (backup-väg in om LinkedIn inte funkar). Att mejla = att
    kontakta → leaden flyttas till pipeline (status 'skickad') och loggas."""
    if not all_emails:
        st.caption("Ingen e-postadress ännu — kör 🔍 Person eller ✉️ E-post, "
                   "eller lägg in en adress under fliken Kontakt.")
        return
    if sent_date:
        st.caption(f"Redan mejlat {sent_date}. Öppna Översikt om du vill "
                   "kontakta igen.")
        return
    # Ärv affärsmodellen från en ev. gjord (och korrigerad) analys så
    # mejlet använder exakt samma benchmark som du godkänt.
    _a = analysis_cache.get(lid) or {}
    to, subj, body, send = render_email_composer(
        f"lead_{lid}", all_emails[0],
        dict(bolag=l.get("bolag", ""), namn=l.get("namn", ""),
             titel=l.get("titel", ""), bransch=l.get("bransch", ""),
             lagerandel=l.get("lagerandel"),
             varulager_msek=l.get("varulager"),
             omsattning_msek=l.get("omsattning"),
             resultat_msek=l.get("resultat"),
             affarsmodell=_a.get("affarsmodell", ""),
             orgnr=l.get("orgnr", ""), website=website),
        to_options=all_emails)
    if not send:
        return
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
        clear_data_cache()
        st.success(f"✅ Mejl skickat till {to} — kontakten är nu i "
                   f"pipeline (kontaktad).")
        st.rerun()
    else:
        st.error(err)
