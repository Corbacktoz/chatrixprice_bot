import os
import math
import logging
from typing import Optional, Tuple

import aiohttp
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    AIORateLimiter,
    CommandHandler,
    ContextTypes,
)

# -------------------- CONFIG --------------------
UPDATE_INTERVAL_SECONDS = 600  # 10 minutes
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# Contrat par défaut (CHATRIX sur BSC) ; modifiable via env TOKEN_ADDRESS
DEFAULT_CONTRACT = "0xAf62c16e46238c14AB8eda78285feb724e7d4444"
CONTRACT_ADDRESS = os.getenv("TOKEN_ADDRESS", DEFAULT_CONTRACT).strip()
PREFERRED_CHAIN = os.getenv("PREFERRED_CHAIN", "bsc").strip().lower()

# -------------------- LOGGING --------------------
logger = logging.getLogger("chatrix-bot")
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# -------------------- UTILS --------------------
def _fmt_num(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "?"
    try:
        n = float(n)
    except Exception:
        return "?"
    absn = abs(n)
    if absn >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if absn >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if absn >= 1_000:
        return f"{n/1_000:.2f}K"
    if absn < 1:
        return f"{n:.8f}".rstrip("0").rstrip(".")
    return f"{n:.2f}"

def _safe_float(d, *path) -> Optional[float]:
    try:
        for p in path:
            d = d[p]
        return float(d) if d is not None else None
    except Exception:
        return None

# -------------------- DATA FETCH --------------------
async def fetch_best_pair(session: aiohttp.ClientSession, address: str, preferred_chain: str = "bsc") -> Optional[dict]:
    url = DEXSCREENER_TOKEN_URL.format(address=address)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError(f"Dexscreener HTTP {r.status}")
        data = await r.json()

    pairs = data.get("pairs", []) or []
    if not pairs:
        return None

    same_chain = [p for p in pairs if (p.get("chainId", "") or "").lower() == preferred_chain.lower()]
    candidates = same_chain if same_chain else pairs

    def score(p):
        liq = _safe_float(p, "liquidity", "usd") or 0.0
        vol = _safe_float(p, "volume", "h24") or 0.0
        return (liq, vol)

    best = max(candidates, key=score)
    return best

def extract_metrics(best: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], str]:
    """
    Retourne: price_usd, change_24h_pct, marketcap_usd, fdv_usd, volume_24h, liquidity_usd, url
    """
    url = best.get("url") or ""
    price_usd = _safe_float(best, "priceUsd")
    change_24h = _safe_float(best, "priceChange", "h24")
    mcap = _safe_float(best, "marketCap")
    fdv = _safe_float(best, "fdv")
    volume_24h = _safe_float(best, "volume", "h24")
    liquidity_usd = _safe_float(best, "liquidity", "usd")
    return price_usd, change_24h, mcap, fdv, volume_24h, liquidity_usd, url

def build_message(price_usd, change_24h, mcap, fdv, volume_24h, liquidity_usd, url: str) -> str:
    chg = f"{change_24h:+.2f}%" if change_24h is not None else "?"
    lines = [
        "<b>CHATRIX (BSC)</b>",
        f"💵 Prix: <b>${_fmt_num(price_usd)}</b>  |  24h: <b>{chg}</b>",
        f"💰 Market Cap: <b>${_fmt_num(mcap) if mcap is not None else (_fmt_num(fdv) if fdv is not None else '?')}</b>",
        f"📊 Volume 24h: <b>${_fmt_num(volume_24h)}</b>",
        f"💦 Liquidité: <b>${_fmt_num(liquidity_usd)}</b>",
    ]
    if url:
        lines.append(f"🔗 <a href='{url}'>Voir sur Dexscreener</a>")
    return "\n".join(lines)

async def compose_message() -> str:
    async with aiohttp.ClientSession() as session:
        best = await fetch_best_pair(session, CONTRACT_ADDRESS, PREFERRED_CHAIN)
        if not best:
            return "Impossible de récupérer les données pour ce token pour le moment."
        price_usd, change_24h, mcap, fdv, volume_24h, liquidity_usd, url = extract_metrics(best)
        return build_message(price_usd, change_24h, mcap, fdv, volume_24h, liquidity_usd, url)

# -------------------- JOB + COMMANDS --------------------
async def send_update(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    try:
        msg = await compose_message()
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info("✅ Sent price update to chat %s", chat_id)
    except Exception as e:
        logger.error("❌ Error while sending update: %s", e)

async def cmd_now(update, context):
    try:
        msg = await compose_message()
        await update.effective_message.reply_html(msg, disable_web_page_preview=False)
        logger.info("✅ Sent manual /now to chat %s", update.effective_chat.id)
    except Exception as e:
        await update.effective_message.reply_text("Erreur lors de l'envoi.")
        logger.error("❌ Error in /now: %s", e)

async def cmd_id(update, _context):
    await update.message.reply_text(f"Votre chat_id est : {update.effective_chat.id}")

async def cmd_start(update, _context):
    await update.message.reply_text(
        "Hello 👋 Je t’enverrai le prix & la marketcap toutes les 10 minutes.\n"
        "• /now pour un envoi immédiat\n"
        "• /id pour afficher le chat_id"
    )

# -------------------- MAIN (synchronE) --------------------
def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

    application = (
        Application.builder()
        .token(bot_token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    # commandes
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("now", cmd_now))
    application.add_handler(CommandHandler("id", cmd_id))

    # planification (JobQueue)
    application.job_queue.run_repeating(
        send_update,
        interval=UPDATE_INTERVAL_SECONDS,
        first=0,  # envoi immédiat au démarrage
        name="price-job",
        data={"chat_id": int(chat_id)},
    )

    logger.info("Bot started. Sending price updates every %s seconds.", UPDATE_INTERVAL_SECONDS)

    # IMPORTANT: ne pas appeler depuis asyncio.run ; PTB gère la boucle
    application.run_polling()  # blocant

if __name__ == "__main__":
    main()
