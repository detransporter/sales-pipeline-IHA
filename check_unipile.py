"""
Unipile-diagnos — hjälper dig sätta upp LinkedIn-inkorgen steg för steg.

    python check_unipile.py

Kollar att DSN + API-nyckel funkar, listar dina anslutna konton (så du hittar
ditt LinkedIn-konto-id), och testar att hämta meddelanden om allt är ifyllt.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from integrations import linkedin_inbox as ib


def main() -> None:
    print("── Unipile-diagnos ──\n")
    print(f"UNIPILE_DSN:        {'✓ satt' if ib.DSN else '✗ SAKNAS'}  ({ib.DSN or '-'})")
    print(f"UNIPILE_API_KEY:    {'✓ satt' if ib.API_KEY else '✗ SAKNAS'}")
    print(f"UNIPILE_ACCOUNT_ID: {'✓ satt' if ib.ACCOUNT_ID else '✗ SAKNAS (fyll i efter steg nedan)'}\n")

    if not (ib.DSN and ib.API_KEY):
        print("→ Fyll i UNIPILE_DSN och UNIPILE_API_KEY i .env först, kör sen igen.")
        return

    print("Hämtar dina anslutna konton...")
    accounts = ib.list_accounts()
    if not accounts:
        print("✗ Inga konton hittades. Kontrollera att DSN/nyckel stämmer och att du")
        print("  anslutit ett LinkedIn-konto i Unipiles dashboard.")
        return

    print(f"✓ {len(accounts)} konto(n) hittade:\n")
    for a in accounts:
        print(f"   id: {a['id']}   namn: {a['name']}   typ: {a['type']}")
    print("\n→ Kopiera id:t för ditt LinkedIn-konto till UNIPILE_ACCOUNT_ID i .env.\n")

    if not ib.ACCOUNT_ID:
        print("(UNIPILE_ACCOUNT_ID är inte ifyllt än — gör det och kör igen för att testa svar.)")
        return

    print("Testar att hämta senaste meddelanden...")
    msgs = ib.fetch_inbound_replies(limit=10)
    print(f"✓ {len(msgs)} inkommande meddelanden hämtade. Allt funkar! 🎉")
    for m in msgs[:3]:
        print(f"   • {m['sender_name']}: {m['text'][:60]}")


if __name__ == "__main__":
    main()
