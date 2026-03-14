#!/usr/bin/env python3
"""Test posting a single Reddit post by ID through the full pipeline."""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from telegram import Bot
from main import make_reddit, resolve_crosspost, handle_submission, load_posted_ids

POST_ID = ""

async def main():
    reddit = make_reddit()
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    try:
        sub = await reddit.submission(id=POST_ID)
        await sub.load()
        sub = await resolve_crosspost(sub, reddit)
        print(f"Title: {sub.title}")
        print(f"ID:    {sub.id}")
        print()
        posted_ids = load_posted_ids()
        result = await handle_submission(bot, sub, posted_ids)
        print("Result:", result)
    finally:
        await reddit.close()

asyncio.run(main())
