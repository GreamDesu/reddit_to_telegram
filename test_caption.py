#!/usr/bin/env python3
"""Test caption splitting: sends a photo with a long caption to verify
that the body is correctly split between the caption and follow-up messages."""

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from telegram import Bot
from telegram.constants import MessageLimit
from main import build_media_caption, split_text, TELEGRAM_CHAT_ID, TELEGRAM_CHANNEL

TEST_IMAGE = "testimg.jpg"

TITLE = "Test post title"
SHORT_BODY = "Short body that fits in the caption easily."
LONG_BODY = ("This is a long body text. " * 60).strip()  # ~1500 chars — won't fit in caption
SHORT_URL = "https://redd.it/test123"


async def send_test(bot: Bot, label: str, body: str) -> None:
    print(f"\n{'='*60}")
    print(f"TEST: {label}")

    t = TITLE
    footer = f"\n{SHORT_URL}\n{TELEGRAM_CHANNEL}"
    caption, overflow = build_media_caption(t, body, footer)

    print(f"Caption length : {len(caption)}/{MessageLimit.CAPTION_LENGTH}")
    print(f"Overflow length: {len(overflow)}")
    print(f"Caption preview: {caption[:120]!r}...")

    with open(TEST_IMAGE, "rb") as f:
        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=f,
            caption=caption,
            parse_mode="HTML",
        )

    if overflow:
        parts = split_text(overflow)
        print(f"Sending {len(parts)} overflow message(s)")
        for part in parts:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=part, parse_mode="HTML")
    else:
        print("No overflow — single message")


async def send_oversized(bot: Bot) -> None:
    """Send a caption that intentionally exceeds 1024 chars to see Telegram's error."""
    print(f"\n{'='*60}")
    print("TEST: Oversized caption (raw, no splitting)")
    oversized = "X" * 1025
    print(f"Caption length: {len(oversized)}")
    try:
        with open(TEST_IMAGE, "rb") as f:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=f,
                caption=oversized,
            )
        print("Sent without error (unexpected)")
    except Exception as e:
        print(f"Error ({type(e).__name__}): {e}")


async def main():
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    await send_test(bot, "No body", "")
    await send_test(bot, "Short body (fits in caption)", SHORT_BODY)
    await send_test(bot, "Long body (overflows caption)", LONG_BODY)
    await send_oversized(bot)
    print("\nDone.")


asyncio.run(main())
