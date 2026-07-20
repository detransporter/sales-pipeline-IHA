"""
Telegram bot för LinkedIn DM Agent.
Kör separat: python tg_bot/bot.py

(Mappen heter tg_bot, inte telegram — en mapp som hette exakt som det
installerade paketet python-telegram-bot skuggade paketet vid import och
gjorde att boten aldrig kunde starta. Döpt om för att fixa det.)

Kommandon:
  /svar [namn] ja|nej       — Markera svar
  /mote [namn] [YYYY-MM-DD] — Boka möte
  /idag                     — Dagens uppföljningslista
  /pipeline                 — Pipeline-sammanfattning
  /dm [namn]                — Generera DM direkt i Telegram
  /chef                     — Kör Sales Chief: förbered dagens jobb
  /inkorg                   — Kolla LinkedIn-svar & förbered förslag
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import functools
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

from database import supabase_client as db
from agents.dm_generator import generate_dm_variants
from agents.followup import get_daily_summary
from agents.qualifier import qualify_reply, CATEGORY_TO_STATUS
from agents.orchestrator import run_day
from agents.inbox_watcher import check_inbox, build_telegram_text as inbox_telegram_text

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ── Helpers ────────────────────────────────────────────────────────────────

async def _reply(update: Update, text: str) -> None:
    await update.message.reply_text(text, parse_mode="Markdown")


def _get_prospect(name_fragment: str) -> dict | None:
    return db.get_prospect_by_name(name_fragment)


def _require_authorized(handler):
    """
    Blockerar kommandon från alla utom TELEGRAM_CHAT_ID.

    CHAT_ID lästes tidigare bara för UTGÅENDE påminnelser — ingen handler
    kontrollerade vem som SKICKADE ett kommando. Läckt bot-token räckte då
    för att trigga en full säljkörning (/chef) eller ändra pipeline-status.
    Saknas CHAT_ID (ej konfigurerat) nekas allt — säkrast att stänga igen
    än att av misstag stå öppen.
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sender_id = str(update.effective_chat.id) if update.effective_chat else None
        if not CHAT_ID or sender_id != str(CHAT_ID):
            logger.warning(f"Obehörigt kommando avvisat från chat_id={sender_id}")
            return  # Tyst avvisning — svarar inte, avslöjar inte att boten finns
        return await handler(update, context)
    return wrapper


# ── Command handlers ───────────────────────────────────────────────────────

@_require_authorized
async def cmd_svar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/svar [namn] ja|nej"""
    args = context.args
    if len(args) < 2:
        await _reply(update, "Användning: `/svar [namn] ja|nej`")
        return

    svar = args[-1].lower()
    name_fragment = " ".join(args[:-1])

    if svar not in ("ja", "nej"):
        await _reply(update, "Svaret måste vara `ja` eller `nej`.")
        return

    prospect = _get_prospect(name_fragment)
    if not prospect:
        await _reply(update, f"Hittade ingen kontakt med namn *{name_fragment}*.")
        return

    new_status = "svar_ja" if svar == "ja" else "svar_nej"
    db.update_prospect_status(prospect["id"], new_status)

    if svar == "ja":
        await _reply(
            update,
            f"✅ *{prospect['namn']}* markerad som `svar_ja`.\n"
            f"Klistra in svaret via Streamlit för AI-analys och nästa steg."
        )
    else:
        await _reply(update, f"❌ *{prospect['namn']}* markerad som `svar_nej`.")


@_require_authorized
async def cmd_mote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mote [namn] [YYYY-MM-DD]"""
    args = context.args
    if len(args) < 2:
        await _reply(update, "Användning: `/mote [namn] [YYYY-MM-DD]`")
        return

    datum = args[-1]
    name_fragment = " ".join(args[:-1])

    try:
        datetime.strptime(datum, "%Y-%m-%d")
    except ValueError:
        await _reply(update, "Datumsformat: YYYY-MM-DD, t.ex. `2026-05-20`")
        return

    prospect = _get_prospect(name_fragment)
    if not prospect:
        await _reply(update, f"Hittade ingen kontakt med namn *{name_fragment}*.")
        return

    db.insert_meeting(prospect["id"], datum)
    db.update_prospect_status(prospect["id"], "mote_bokat")
    await _reply(update, f"📅 Möte bokat med *{prospect['namn']}* den {datum}!")


@_require_authorized
async def cmd_idag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/idag — Dagens uppföljningslista"""
    try:
        summary = get_daily_summary()
    except Exception as e:
        await _reply(update, f"Fel: {e}")
        return

    meetings_text = ""
    for m in summary["meetings_today"]:
        p = m.get("prospects") or {}
        meetings_text += f"\n  • {p.get('namn', '?')} @ {p.get('bolag', '?')}"

    text = (
        "📋 *Dagens prospektering:*\n"
        f"• Skicka DM till: {summary['new_to_send']} nya kontakter\n"
        f"• Följ upp: {summary['followups_due']} kontakter\n"
        f"• Inväntar svar: {summary['awaiting_reply']} kontakter\n"
        f"• Möten idag: {len(summary['meetings_today'])}"
        + meetings_text
    )
    await _reply(update, text)


@_require_authorized
async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pipeline — Pipeline-sammanfattning"""
    try:
        stats = db.get_pipeline_stats()
    except Exception as e:
        await _reply(update, f"Fel: {e}")
        return

    text = (
        "📊 *Pipeline-sammanfattning:*\n"
        f"• Totalt kontaktade: {stats['kontaktade']}\n"
        f"• Fått svar: {stats['svar']}\n"
        f"• Möten bokade: {stats['moten']}\n"
        f"• Konvertering: {stats['konvertering']}%"
    )
    await _reply(update, text)


@_require_authorized
async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dm [namn] — Generera DM direkt"""
    if not context.args:
        await _reply(update, "Användning: `/dm [namn]`")
        return

    name_fragment = " ".join(context.args)
    prospect = _get_prospect(name_fragment)
    if not prospect:
        await _reply(update, f"Hittade ingen kontakt med namn *{name_fragment}*.")
        return

    await _reply(update, f"⏳ Genererar DM för *{prospect['namn']}*...")

    try:
        variants = generate_dm_variants(
            prospect["namn"],
            prospect["titel"],
            prospect["bolag"],
            prospect["bransch"],
        )
    except Exception as e:
        await _reply(update, f"Claude API-fel: {e}")
        return

    text = (
        f"✉️ *DM:s för {prospect['namn']}:*\n\n"
        f"*A (kort):*\n{variants.get('variant_a', '')}\n\n"
        f"*B (bolag):*\n{variants.get('variant_b', '')}\n\n"
        f"*C (bransch):*\n{variants.get('variant_c', '')}"
    )
    await _reply(update, text)


@_require_authorized
async def cmd_chef(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chef — Kör Sales Chief (orchestratorn) nu och förbered dagens jobb."""
    await _reply(update, "🧠 Sales Chief jobbar... (förbereder DM, uppföljningar & leads)")
    loop = asyncio.get_event_loop()
    try:
        # run_day gör flera API-anrop — kör i tråd så vi inte blockerar boten
        result = await loop.run_in_executor(None, lambda: run_day(run_type="manual"))
    except Exception as e:
        await _reply(update, f"Fel i orchestratorn: {e}")
        return
    await _reply(update, result["telegram"])


@_require_authorized
async def cmd_inkorg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/inkorg — Kolla LinkedIn-svar nu och förbered förslag."""
    await _reply(update, "💬 Kollar inkorgen...")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, check_inbox)
    except Exception as e:
        await _reply(update, f"Fel i inkorg-agenten: {e}")
        return
    await _reply(update, inbox_telegram_text(result))


# ── Daily reminder (scheduled job) ────────────────────────────────────────

async def send_daily_reminder(app) -> None:
    """Send daily 08:00 briefing to configured chat."""
    try:
        summary = get_daily_summary()
    except Exception:
        return

    meetings_text = ""
    for m in summary["meetings_today"]:
        p = m.get("prospects") or {}
        meetings_text += f"\n  • {p.get('namn', '?')} @ {p.get('bolag', '?')}"

    text = (
        "📋 *Dagens prospektering:*\n"
        f"• Skicka DM till: {summary['new_to_send']} nya kontakter\n"
        f"• Följ upp: {summary['followups_due']} kontakter\n"
        f"• Inväntar svar: {summary['awaiting_reply']} kontakter\n"
        f"• Möten idag: {len(summary['meetings_today'])}"
        + meetings_text
    )

    if CHAT_ID:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN är inte satt i .env")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("svar", cmd_svar))
    app.add_handler(CommandHandler("mote", cmd_mote))
    app.add_handler(CommandHandler("idag", cmd_idag))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("dm", cmd_dm))
    app.add_handler(CommandHandler("chef", cmd_chef))
    app.add_handler(CommandHandler("inkorg", cmd_inkorg))

    logger.info("Telegram bot startad. Lyssnar på kommandon...")
    app.run_polling()


if __name__ == "__main__":
    main()
