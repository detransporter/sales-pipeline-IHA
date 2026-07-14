"""📥 Kontakter — skapa manuellt eller importera från Excel."""

import streamlit as st

from utils.excel_parser import parse_excel, dataframe_to_prospect_records
from agents.prospecting import score_dataframe
from database import supabase_client as db
from views.shared import KONTAKT_KATEGORIER

# SQL David kan klistra in i Supabase om 'kategori'-kolumnen saknas.
_KATEGORI_SQL = ("ALTER TABLE sales_chief.prospects "
                 "ADD COLUMN IF NOT EXISTS kategori text;")


def render():
    st.title("📥 Kontakter")
    st.caption("Skapa en kontakt manuellt (t.ex. någon du redan känner) eller "
               "importera många från en Excel-fil.")

    _render_manual_create()
    st.divider()
    _render_excel_import()


def _render_manual_create():
    st.subheader("➕ Skapa en kontakt manuellt")
    with st.form("manual_contact", clear_on_submit=True):
        r1c1, r1c2 = st.columns(2)
        bolag = r1c1.text_input("Bolag *", placeholder="Exempel AB")
        kategori = r1c2.selectbox("Kategori", KONTAKT_KATEGORIER, index=0,
                                  help="Så du kan urskilja vem som är vad.")
        r2c1, r2c2 = st.columns(2)
        namn = r2c1.text_input("Namn", placeholder="Anna Lindqvist")
        titel = r2c2.text_input("Roll/titel", placeholder="Inköpschef")
        r3c1, r3c2 = st.columns(2)
        email = r3c1.text_input("E-post", placeholder="anna@exempel.se")
        telefon = r3c2.text_input("Telefon", placeholder="+46 70 123 45 67")
        r4c1, r4c2 = st.columns(2)
        linkedin = r4c1.text_input("LinkedIn-URL", placeholder="https://linkedin.com/in/...")
        bransch = r4c2.text_input("Bransch", placeholder="Tillverkning")
        website = st.text_input("Hemsida", placeholder="https://exempel.se")
        anteckning = st.text_area("Anteckning (valfritt)", height=70,
                                  placeholder="Hur ni känns, vad ni pratat om ...")

        submitted = st.form_submit_button("💾 Skapa kontakt", type="primary")
        if submitted:
            if not bolag.strip():
                st.warning("Bolag är obligatoriskt.")
                return
            record = {
                "bolag": bolag.strip(),
                "namn": namn.strip() or None,
                "titel": titel.strip() or None,
                "kategori": kategori,
                "email": email.strip() or None,
                "telefon": telefon.strip() or None,
                "linkedin_url": linkedin.strip() or None,
                "bransch": bransch.strip() or None,
                "website": website.strip() or None,
                "extra_info": anteckning.strip() or None,
                "status": "ej_kontaktad",
                "score": 5,
            }
            try:
                saved = db.insert_prospect(record)
                st.success(f"✅ {bolag.strip()} skapad som **{kategori}** — "
                           "syns nu i 📊 Översikt.")
                # Sparades kategorin faktiskt? (kolumnen kan saknas i en äldre DB)
                if saved and "kategori" not in saved:
                    st.warning(
                        "Kontakten sparades, men **kategorin kunde inte lagras** — "
                        "kolumnen saknas i databasen. Klistra in detta i Supabase → "
                        "SQL Editor en gång, så funkar kategorier överallt:")
                    st.code(_KATEGORI_SQL, language="sql")
            except Exception as e:
                st.error(f"Kunde inte skapa kontakt: {e}")


def _render_excel_import():
    st.subheader("📄 Importera från Excel")
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

        kat = st.selectbox("Kategori för alla importerade", KONTAKT_KATEGORIER, index=0)
        if st.button("💾 Spara till Supabase", type="primary"):
            records = dataframe_to_prospect_records(df_scored)
            for i, row in df_scored.iterrows():
                records[i]["score"] = int(row["score"])
                records[i]["kategori"] = kat
            try:
                saved = db.insert_prospects(records)
                st.success(f"✅ {len(saved)} kontakter sparade som {kat}!")
            except Exception as e:
                msg = str(e)
                if "kategori" in msg.lower() or "column" in msg.lower():
                    st.warning("Kolumnen 'kategori' saknas i databasen. Kör detta i "
                               "Supabase → SQL Editor en gång, importera sedan igen:")
                    st.code(_KATEGORI_SQL, language="sql")
                else:
                    st.error(f"Supabase-fel: {e}")
