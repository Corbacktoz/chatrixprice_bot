import os
import math
import time
import random
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any

import aiohttp
from telegram.constants import ParseMode
from telegram.ext import Application, AIORateLimiter, CommandHandler, ContextTypes

# -------------------- PARAMS --------------------
UPDATE_INTERVAL_SECONDS = 1800  # 30 minutes
MIN_REFRESH_SECONDS = 60        # cache anti-spam
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
PREFERRED_CHAIN = os.getenv("PREFERRED_CHAIN", "bsc").strip().lower()

TOKENS = [
    {
        "address": "0xAf62c16e46238c14AB8eda78285feb724e7d4444",
        "label": "CHATRIX",
        "holdings": float(os.getenv("HOLDINGS_CHATRIX", "3239008")),
    },
    {
        "address": "0x9f6c24232f1Bba6ef47BCb81b9b9434aCDB94444",
        "label": "TKN2",
        "holdings": float(os.getenv("HOLDINGS_T2", "0")),
    },
    {
        "address": "0xe939C153e56136691Dca84fC92E8fFBb46854444",
        "label": "TKN3",
        "holdings": float(os.getenv("HOLDINGS_T3", "0")),
    },
]

# cache par token
_cache: Dict[str, Dict[str, Any]] = {}

# -------------------- LOGGING --------------------
logger = logging.getLogger("chatrix-bot")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# -------------------- UTILS --------------------
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
    if absn < 1:
        return f"{n:.8f}".rstrip("0").rstrip(".")
    return f"{n:.4f}" if n < 10 else f"{n:.2f}"

def _fmt_int(n: float) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)

def _safe_float(d, *path) -> Optional[float]:
    try:
        for p in path:
            d = d[p]
        return float(d) if d is not None else None
    except Exception:
        return None

# -------------------- FETCH --------------------
async def fetch_best_pair(session: aiohttp.ClientSession, address: str, preferred_chain: str = "bsc") -> Optional[dict]:
    url = DEXSCREENER_TOKEN_URL.format(address=address)
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": "chatrix-price-bot/1.2"}
    backoff = 1.0

    for attempt in range(5):
        async with session.get(url, timeout=timeout, headers=headers) as r:
            if r.status == 200:
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

                return max(candidates, key=score)

            if r.status == 429:
                ra = r.headers.get("Retry-After")
                sleep_s = float(ra) if ra and ra.isdigit() else backoff + random.uniform(0, 0.5)
                logger.warning("Dexscreener 429 for %s. Retry in %.2fs (try %s/5)", address, sleep_s, attempt + 1)
                await asyncio.sleep(sleep_s)
                backoff *= 2
                continue

            if 500 <= r.status < 600:
                sleep_s = backoff + random.uniform(0, 0.5)
                logger.warning("Dexscreener %s for %s. Retry in %.2fs (try %s/5)", r.status, address, sleep_s, attempt + 1)
                await asyncio.sleep(sleep_s)
                backoff *= 2
                continue

            raise RuntimeError(f"Dexscreener HTTP {r.status} for {address}")

    raise RuntimeError(f"Dexscreener rate limited for {address}")

def extract_metrics(best: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], str, str]:
    url = best.get("url") or ""
    price_usd = _safe_float(best, "priceUsd")
    change_24h = _safe_float(best, "priceChange", "h24")
    mcap = _safe_float(best, "marketCap")
    fdv = _safe_float(best, "fdv")

    base_sym = (best.get("baseToken", {}) or {}).get("symbol") or ""
    quote_sym = (best.get("quoteToken", {}) or {}).get("symbol") or ""
    chain = (best.get("chainId") or "").upper()
    pretty = base_sym or "Token"
    if chain:
        pretty += f" ({chain})"
    if quote_sym:
        pretty += f"/{quote_sym}"
    return price_usd, change_24h, mcap, fdv, url, pretty

def build_token_section(label: str, price_usd, change_24h, mcap, fdv, url: str, holdings: float) -> Tuple[str, float]:
    chg = f"{change_24h:+.2f}%" if change_24h is not None else "?"
    if price_usd is not None:
        value = holdings * price_usd
        holdings_line = f"👛 { _fmt_int(holdings) }  →  <b>${_fmt_num(value)}</b>"
    else:
        value = 0.0
        holdings_line = f"👛 { _fmt_int(holdings) }  →  <b>?</b>"

    lines = [
        f"<b>{label}</b>",
        f"  • Prix: <b>${_fmt_num(price_usd)}</b>  |  24h: <b>{chg}</b>",
        f"  • Market Cap: <b>${_fmt_num(mcap) if mcap is not None else (_fmt_num(fdv) if fdv is not None else '?')}</b>",
        f"  • Valeur tes tokens: {holdings_line}",
    ]
    if url:
        lines.append(f"  • <a href='{url}'>Dexscreener</a>")
    return "\n".join(lines), value

async def compose_message() -> str:
    now = time.time()
    async with aiohttp.ClientSession() as session:
        sections = []
        total_value = 0.0

        for t in TOKENS:
            addr = t["address"]
            label = t["label"]
            holdings = float(t["holdings"])

            c = _cache.get(addr)
            if c and (now - c.get("ts", 0)) < MIN_REFRESH_SECONDS:
                sections.append(c["msg"])
                total_value += c.get("value", 0.0)
                continue

            best = await fetch_best_pair(session, addr, PREFERRED_CHAIN)
            if not best:
                msg = f"<b>{label}</b>\n  • Données indisponibles pour le moment."
                value = 0.0
            else:
                price_usd, change_24h, mcap, fdv, url, pretty_name = extract_metrics(best)
                shown_label = pretty_name if pretty_name.lower() != "token" else label
                msg, value = build_token_section(shown_label, price_usd, change_24h, mcap, fdv, url, holdings)

            sections.append(msg)
            _cache[addr] = {"ts": now, "msg": msg, "value": value}
            total_value += value

    sections.append(f"\n<b>💼 Total (3 tokens):</b> <b>${_fmt_num(total_value)}</b>")
    return "\n\n".join(sections)

# -------------------- JOB + COMMANDS --------------------
async def send_update(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    try:
        msg = await compose_message()
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        logger.info("✅ Sent price update to chat %s", chat_id)
    except Exception as e:
        logger.error("❌ Error in send_update: %s", e)

async def cmd_now(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = await compose_message()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    except Exception as e:
        await update.message.reply_text(f"Erreur: {e}")

async def main():
	token = os.getenv("TELEGRAM_BOT_TOKEN")
	chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant")

    app = Application.builder().token(token).rate_limiter(AIORateLimiter()).build()

    app.add_handler(CommandHandler("now", cmd_now))

    app.job_queue.run_repeating(send_update, interval=UPDATE_INTERVAL_SECONDS, first=5, data={"chat_id": chat_id})

    logger.info("Bot started. Sending price updates every %s seconds.", UPDATE_INTERVAL_SECONDS)
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
