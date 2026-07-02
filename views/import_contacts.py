"""📥 Importera kontakter — ladda upp Excel med LinkedIn-kontakter."""

import streamlit as st

from utils.excel_parser import parse_excel, dataframe_to_prospect_records
from agents.prospecting import score_dataframe
from database import supabase_client as db


def render():
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
