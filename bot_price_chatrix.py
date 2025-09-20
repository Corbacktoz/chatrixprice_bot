import os
import asyncio
import logging
from typing import Optional, Tuple

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application, AIORateLimiter, CallbackContext

# -------------------- CONFIG --------------------
TOKEN_ADDRESS = "0xAf62c16e46238c14AB8eda78285feb724e7d4444"  # CHATRIX on BSC
DEXSCREENER_URL = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
UPDATE_INTERVAL_SECONDS = 600  # 10 minutes
TOKEN_SYMBOL = "CHATRIX"
CHAIN_NAME = "BSC"
# ------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("chatrix-bot")


# ------------- HTTP / DATA HELPERS -------------
async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, timeout=20) as r:
            if r.status != 200:
                logger.warning("HTTP %s on %s", r.status, url)
                return None
            return await r.json()
    except Exception as e:
        logger.exception("Failed to fetch %s: %s", url, e)
        return None


def pick_best_pair(data: dict) -> Optional[dict]:
    """
    Dexscreener peut retourner plusieurs paires. On prend celle avec la plus forte liquidité USD.
    """
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p.get("liquidity", {}).get("usd") or 0), reverse=True)
    return pairs[0]


def _fmt_num(n: Optional[float]) -> str:
    if n is None:
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
    return f"{n:.6f}" if absn < 1 else f"{n:.4f}"


def extract_metrics(best: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], str]:
    """
    Retourne (price_usd, change_24h_pct, marketcap_usd, fdv_usd, url)
    """
    url = best.get("url") or ""
    # price
    try:
        price_usd = float(best.get("priceUsd")) if best.get("priceUsd") is not None else None
    except Exception:
        price_usd = None
    # 24h change
    try:
        change_24h = float(best.get("priceChange", {}).get("h24")) if best.get("priceChange") else None
    except Exception:
        change_24h = None
    # market cap & fdv
    try:
        mcap = float(best.get("marketCap")) if best.get("marketCap") is not None else None
    except Exception:
        mcap = None
    try:
        fdv = float(best.get("fdv")) if best.get("fdv") is not None else None
    except Exception:
        fdv = None

    return price_usd, change_24h, mcap, fdv, url


def build_message(price_usd: Optional[float], change_24h: Optional[float],
                  mcap: Optional[float], fdv: Optional[float], url: str) -> str:
    chg = f"{change_24h:+.2f}%" if change_24h is not None else "?"
    price_line = f"💵 Prix: <b>${_fmt_num(price_usd)}</b>  |  24h: <b>{chg}</b>"

    if mcap is not None:
        mc_line = f"💰 Market Cap: <b>${_fmt_num(mcap)}</b>"
    elif fdv is not None:
        mc_line = f"💰 FDV (proxy): <b>${_fmt_num(fdv)}</b>"
    else:
        mc_line = "💰 Market Cap: <b>?</b>"

    lines = [
        f"<b>{TOKEN_SYMBOL} ({CHAIN_NAME})</b>",
        price_line,
        mc_line,
    ]
    if url:
        lines.append(f"🔗 <a href='{url}'>Voir sur Dexscreener</a>")
    return "\n".join(lines)


# ---------------- TELEGRAM JOB -----------------
async def send_update(context: CallbackContext) -> None:
    chat_id = context.job.data["chat_id"]
    bot: Bot = context.bot

    async with aiohttp.ClientSession() as session:
        data = await fetch_json(session, DEXSCREENER_URL)
        if not data:
            await bot.send_message(chat_id=chat_id, text=f"❌ Impossible de récupérer les données Dexscreener pour {TOKEN_SYMBOL}.")
            return

        best = pick_best_pair(data)
        if not best:
            await bot.send_message(chat_id=chat_id, text=f"❌ Aucune paire Dexscreener trouvée pour {TOKEN_SYMBOL}.")
            return

        price_usd, change_24h, mcap, fdv, url = extract_metrics(best)
        msg = build_message(price_usd, change_24h, mcap, fdv, url)
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=False)


# --------------------- MAIN --------------------
async def main():
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

    # Lance immédiatement puis toutes les 10 minutes
    application.job_queue.run_repeating(
        send_update,
        interval=UPDATE_INTERVAL_SECONDS,
        first=0,  # envoie un message tout de suite
        data={"chat_id": int(chat_id)},
        name="price-job",
    )

    logger.info("Bot started. Sending price updates every %s seconds.", UPDATE_INTERVAL_SECONDS)

    await application.initialize()
    await application.start()

    try:
        # Maintenir le bot en vie
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
