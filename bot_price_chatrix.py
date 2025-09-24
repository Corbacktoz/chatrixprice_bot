import os
import asyncio
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    AIORateLimiter,
    CommandHandler,
    ContextTypes,
)

# ------------------ Config & logging ------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("chatrix-bot")

DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{}"

# ---- Tokens à suivre ----
TOKEN_T1_ADDR = "0xAf62c16e46238c14AB8eda78285feb724e7d4444"  # Chatrix
TOKEN_T2_ADDR = "0x9f6c24232f1Bba6ef47BCb81b9b9434aCDB94444"
TOKEN_T3_ADDR = "0xe939C153e56136691Dca84fC92E8fFBb46854444"

TOKENS: List[Tuple[str, str, str]] = [
    ("Chatrix", TOKEN_T1_ADDR, os.getenv("HOLDINGS_AMOUNT", "0").strip()),
    ("T2", TOKEN_T2_ADDR, os.getenv("HOLDINGS_T2", "0").strip()),
    ("T3", TOKEN_T3_ADDR, os.getenv("HOLDINGS_T3", "0").strip()),
]

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SEND_INTERVAL_MIN = 30  # minutes

# ------------------ Helpers ------------------


def _to_float(x: str, default: float = 0.0) -> float:
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return default


def _fmt_usd(v: float) -> str:
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:,.4f}"
    return f"${v:.8f}"


async def fetch_price_usd(session: aiohttp.ClientSession, token_addr: str) -> Optional[float]:
    url = DEX_API.format(token_addr)
    backoff = 2.0
    for attempt in range(6):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.7, 20.0)
                    continue
                r.raise_for_status()
                data = await r.json()
        except Exception as e:
            log.warning("Dexscreener request failed (try %s): %s", attempt + 1, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 20.0)
            continue

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        for p in pairs:
            price = p.get("priceUsd")
            if price is not None:
                return _to_float(price, None)
        return None

    return None


async def build_message() -> str:
    rows: List[str] = []
    total_usd = 0.0

    async with aiohttp.ClientSession() as session:
        for label, addr, amount_str in TOKENS:
            amount = _to_float(amount_str, 0.0)
            price = await fetch_price_usd(session, addr)

            if price is None:
                rows.append(f"• {label}: prix indisponible ❌")
                continue

            value = amount * price
            total_usd += value

            rows.append(
                f"• {label}: prix {_fmt_usd(price)} — holdings {amount:,.0f} — valeur {_fmt_usd(value)}"
            )

    rows.append("—" * 32)
    rows.append(f"Total (3 tokens) : {_fmt_usd(total_usd)}")

    return "\n".join(rows)


# ------------------ Telegram Handlers ------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "Salut ! J’enverrai automatiquement les prix toutes les 30 minutes.\nTape /now pour un update immédiat."
    await context.bot.send_message(chat_id=CHAT_ID or update.effective_chat.id, text=msg)


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text = await build_message()
        await context.bot.send_message(chat_id=CHAT_ID or update.effective_chat.id, text=text)
    except Exception as e:
        log.exception("Error in /now: %s", e)
        await context.bot.send_message(chat_id=CHAT_ID or update.effective_chat.id, text=f"Erreur: {e}")


async def job_send_periodic(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text = await build_message()
        await context.bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        log.exception("Periodic send error: %s", e)


# ------------------ Main ------------------


async def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))

    app.job_queue.run_repeating(
        job_send_periodic, interval=timedelta(minutes=SEND_INTERVAL_MIN), first=0
    )

    log.info("Bot started. Sending price updates every %s seconds.", SEND_INTERVAL_MIN * 60)
    await app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    asyncio.run(main())
