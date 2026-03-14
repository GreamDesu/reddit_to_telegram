#!/usr/bin/env python3
"""Telegram bot that reposts top daily posts from a subreddit on a schedule."""

import asyncio
import html
import json
import logging
import os
import shutil
import tempfile
import time
import tomllib
import traceback
from pathlib import Path

import asyncpraw
import asyncprawcore
import yt_dlp
from asyncpraw.models import Submission
from dotenv import load_dotenv
from telegram import Bot, InputMediaPhoto
from telegram.constants import MessageLimit
from telegram.error import TelegramError, TimedOut

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "config.toml"))

def _load_config() -> dict:
    if not _CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Config file not found: {_CONFIG_FILE}. "
            "Copy config.toml and fill in your settings."
        )
    with open(_CONFIG_FILE, "rb") as f:
        return tomllib.load(f)

_cfg = _load_config()

# Secrets — from .env only
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD")

# Settings — from config.toml
SUBREDDIT         = _cfg["reddit"]["subreddit"]
FETCH_LIMIT       = _cfg["reddit"].get("fetch_limit", 100)
REDDIT_USER_AGENT = _cfg["reddit"].get("user_agent", "telegram-reddit-bot/1.0")
TELEGRAM_CHANNEL  = _cfg["telegram"]["channel"]
POST_INTERVAL     = int(_cfg["bot"]["post_interval_minutes"]) * 60
POSTED_IDS_FILE   = Path(_cfg["bot"].get("posted_ids_file", "posted_ids.json"))
MAX_VIDEO_DURATION = int(_cfg["video"]["max_duration_minutes"]) * 60
DOWNLOAD_TIMEOUT   = int(_cfg["video"]["download_timeout_minutes"]) * 60
MAX_FILE_SIZE      = int(_cfg["video"]["max_file_size_mb"]) * 1024 * 1024
HISTORY_TTL        = int(_cfg["bot"].get("history_ttl_hours", 48)) * 3600


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_posted_ids() -> set[str]:
    if not POSTED_IDS_FILE.exists():
        return set()
    try:
        with open(POSTED_IDS_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        logger.warning("posted_ids.json is empty or corrupt, starting fresh.")
        return set()

    # Migrate from old flat-list format — treat all as added right now
    if isinstance(data, list):
        data = {id_: time.time() for id_ in data}

    # Prune entries older than HISTORY_TTL
    cutoff = time.time() - HISTORY_TTL
    pruned = {id_: ts for id_, ts in data.items() if ts > cutoff}
    if len(pruned) < len(data):
        logger.info("Pruned %d expired IDs from history (%d remaining).", len(data) - len(pruned), len(pruned))
        with open(POSTED_IDS_FILE, "w") as f:
            json.dump(pruned, f, indent=2)

    return set(pruned.keys())


def save_posted_ids(ids: set[str]) -> None:
    # Reload existing timestamps so we don't overwrite them with a fresh time
    existing: dict[str, float] = {}
    if POSTED_IDS_FILE.exists():
        try:
            with open(POSTED_IDS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                existing = data
        except (json.JSONDecodeError, ValueError):
            pass

    now = time.time()
    merged = {id_: existing.get(id_, now) for id_ in ids}
    with open(POSTED_IDS_FILE, "w") as f:
        json.dump(merged, f, indent=2)


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

def make_reddit() -> asyncpraw.Reddit:
    return asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        # Read-only mode — no username/password needed for public subreddits.
        # asyncpraw automatically uses application-only OAuth (2 legged).
        ratelimit_seconds=300,  # wait up to 5 min if rate-limited before raising
    )


async def fetch_top_posts(reddit: asyncpraw.Reddit) -> list[Submission]:
    subreddit = await reddit.subreddit(SUBREDDIT)
    posts = []
    async for post in subreddit.top(time_filter="day", limit=FETCH_LIMIT):
        posts.append(await resolve_crosspost(post, reddit))
    return posts


async def resolve_crosspost(sub: Submission, reddit: asyncpraw.Reddit) -> Submission:
    """If this is a crosspost, return the original post (which has the media)."""
    parents = getattr(sub, "crosspost_parent_list", None)
    if not parents:
        return sub
    parent_data = parents[0]
    parent_id = parent_data.get("id") or parent_data.get("name", "").removeprefix("t3_")
    if not parent_id:
        return sub
    # If the original post is already removed, flag the crosspost and bail early
    # (avoids an extra API call and correctly skips it downstream)
    if (
        parent_data.get("removed_by_category")
        or parent_data.get("selftext", "") in ("[removed]", "[deleted]")
    ):
        sub._parent_removed = True
        return sub
    try:
        parent = await reddit.submission(id=parent_id)
        # Keep the crosspost's own title so it stays in context for the subreddit.
        # Also propagate NSFW flag: the crosspost or embedded parent data may carry
        # over_18=True even when the fetched parent object doesn't (subreddit mismatch).
        if getattr(sub, "over_18", False) or parent_data.get("over_18"):
            parent.over_18 = True
        parent.title = sub.title
        return parent
    except Exception as e:
        logger.warning("Could not resolve crosspost parent %s: %s", parent_id, e)
        return sub


def is_removed(sub: Submission) -> bool:
    """Return True if the post was removed by a moderator, admin, or deleted by the author."""
    if getattr(sub, "_parent_removed", False):
        return True
    if getattr(sub, "removed_by_category", None):
        return True
    if getattr(sub, "selftext", "") in ("[removed]", "[deleted]"):
        return True
    return False


def is_nsfw(sub: Submission) -> bool:
    return bool(getattr(sub, "over_18", False))


def get_post_type(sub: Submission) -> str:
    url = sub.url
    hint = getattr(sub, "post_hint", "")

    if sub.is_self:
        return "text"
    if sub.is_video or hint == "hosted:video" or "v.redd.it" in url:
        return "video"
    if getattr(sub, "is_gallery", False):
        return "gallery"
    if url.endswith(".gif") or url.endswith(".gifv"):
        return "gif"
    if hint == "image" or url.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    return "link"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_text(text: str, limit: int = MessageLimit.MAX_TEXT_LENGTH) -> list[str]:
    """Split text into chunks that fit within Telegram's message limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return chunks


def _download_video_sync(url: str) -> str | None:
    """Blocking yt-dlp download. Call via asyncio.to_thread."""
    tmp_dir = tempfile.mkdtemp()
    out_tmpl = os.path.join(tmp_dir, "video.%(ext)s")
    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_VIDEO_DURATION}"),
        "ignoreerrors": False,
        "abort_on_unavailable_fragments": False,
    }
    if REDDIT_USERNAME and REDDIT_PASSWORD:
        ydl_opts["username"] = REDDIT_USERNAME
        ydl_opts["password"] = REDDIT_PASSWORD
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            ext = info.get("ext", "mp4") if info else "mp4"
        out_path = os.path.join(tmp_dir, f"video.{ext}")
        if os.path.exists(out_path) and 0 < os.path.getsize(out_path) <= MAX_FILE_SIZE:
            return out_path
        logger.warning("yt-dlp output missing or too large: %s", out_path)
    except yt_dlp.utils.DownloadError as e:
        logger.warning("yt-dlp skipped or failed for %s: %s", url, e)
    except Exception as e:
        logger.warning("yt-dlp unexpected error for %s: %s", url, e)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


async def download_video(url: str) -> str | None:
    """Download video in a thread with a hard timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_download_video_sync, url),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Video download timed out after %ds: %s", DOWNLOAD_TIMEOUT, url)
        return None


def get_gallery_urls(sub: Submission) -> list[str]:
    """Extract ordered image URLs from a Reddit gallery post."""
    media_metadata: dict = getattr(sub, "media_metadata", {}) or {}
    gallery_data: dict = getattr(sub, "gallery_data", {}) or {}
    gallery_items: list = gallery_data.get("items", [])
    urls = []
    for item in gallery_items:
        media_id = item.get("media_id")
        if not media_id or media_id not in media_metadata:
            continue
        meta = media_metadata[media_id]
        if meta.get("status") != "valid":
            continue
        s = meta.get("s", {})
        img_url = s.get("u") or s.get("gif")
        if img_url:
            urls.append(img_url.replace("&amp;", "&"))
    return urls


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def post_to_telegram(bot: Bot, sub: Submission) -> bool:
    title = sub.title
    url = sub.url
    selftext = sub.selftext or ""
    post_type = get_post_type(sub)
    spoiler = bool(getattr(sub, "spoiler", False))
    short_url = f"https://redd.it/{sub.id}"

    # Caption for media posts (Telegram limit: 1024 chars, HTML)
    footer = f"\n{short_url}\n{TELEGRAM_CHANNEL}"
    t = html.escape(title)
    max_title = 1024 - len(footer)
    caption = (t if len(t) <= max_title else t[:max_title - 1] + "…") + footer

    logger.info("Posting [%s%s] %s", post_type, " spoiler" if spoiler else "", title[:60])

    try:
        # ------------------------------------------------------------------
        # Text post
        # ------------------------------------------------------------------
        if post_type == "text":
            body = html.escape(selftext.strip())
            if spoiler:
                body = f"<tg-spoiler>{body}</tg-spoiler>"
            text_footer = f"\n{short_url}\n{TELEGRAM_CHANNEL}"
            full = f"<b>{t}</b>\n\n{body}{text_footer}" if body else f"<b>{t}</b>{text_footer}"
            for part in split_text(full):
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=part,
                    parse_mode="HTML",
                )

        # ------------------------------------------------------------------
        # Image post
        # ------------------------------------------------------------------
        elif post_type == "image":
            try:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=url,
                    caption=caption,
                    parse_mode="HTML",
                    has_spoiler=spoiler,
                )
            except TelegramError as e:
                logger.warning("send_photo failed (%s), sending link instead", e)
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                    parse_mode="HTML",
                )

        # ------------------------------------------------------------------
        # GIF post
        # ------------------------------------------------------------------
        elif post_type == "gif":
            try:
                await bot.send_animation(
                    chat_id=TELEGRAM_CHAT_ID,
                    animation=url,
                    caption=caption,
                    parse_mode="HTML",
                    has_spoiler=spoiler,
                )
            except TelegramError as e:
                logger.warning("send_animation failed (%s), sending link instead", e)
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                    parse_mode="HTML",
                )

        # ------------------------------------------------------------------
        # Video post (Reddit-hosted or external)
        # ------------------------------------------------------------------
        elif post_type == "video":
            download_url = f"https://www.reddit.com{sub.permalink}" if "v.redd.it" in url else url
            video_path = await download_video(download_url)
            if video_path:
                try:
                    with open(video_path, "rb") as vf:
                        await bot.send_video(
                            chat_id=TELEGRAM_CHAT_ID,
                            video=vf,
                            caption=caption,
                            parse_mode="HTML",
                            supports_streaming=True,
                            has_spoiler=spoiler,
                        )
                except TelegramError as e:
                    logger.warning("send_video failed (%s), sending link instead", e)
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                        parse_mode="HTML",
                    )
                finally:
                    try:
                        shutil.rmtree(os.path.dirname(video_path), ignore_errors=True)
                    except OSError:
                        pass
            else:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                    parse_mode="HTML",
                )

        # ------------------------------------------------------------------
        # Gallery post
        # ------------------------------------------------------------------
        elif post_type == "gallery":
            image_urls = get_gallery_urls(sub)
            if image_urls:
                batch = image_urls[:10]
                media = [InputMediaPhoto(media=u, has_spoiler=spoiler) for u in batch]
                media[0] = InputMediaPhoto(
                    media=batch[0],
                    caption=caption,
                    parse_mode="HTML",
                    has_spoiler=spoiler,
                )
                try:
                    await bot.send_media_group(
                        chat_id=TELEGRAM_CHAT_ID,
                        media=media,
                        read_timeout=60,
                        write_timeout=60,
                    )
                except TimedOut:
                    logger.warning("send_media_group timed out; assuming it went through")
                except TelegramError as e:
                    logger.warning("send_media_group failed (%s), sending link", e)
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                        parse_mode="HTML",
                    )
            else:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                    parse_mode="HTML",
                )

        # ------------------------------------------------------------------
        # External link
        # ------------------------------------------------------------------
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"<b>{t}</b>\n{url}\n{short_url}\n{TELEGRAM_CHANNEL}",
                parse_mode="HTML",
            )

        return True

    except Exception as e:
        logger.error("Failed to post %s: %s", sub.id, e)
        return False


async def handle_submission(bot: Bot, sub: Submission, posted_ids: set[str]) -> str:
    """Run one submission through the full filter + post pipeline.

    Mutates posted_ids and persists it for skip/post outcomes.
    Returns: 'posted' | 'failed' | 'removed' | 'nsfw' | 'already_posted'
    """
    if sub.id in posted_ids:
        return "already_posted"
    if is_removed(sub):
        logger.info("Skipping removed post %s: %s", sub.id, sub.title[:60])
        posted_ids.add(sub.id)
        save_posted_ids(posted_ids)
        return "removed"
    if is_nsfw(sub):
        logger.info("Skipping NSFW post %s: %s", sub.id, sub.title[:60])
        posted_ids.add(sub.id)
        save_posted_ids(posted_ids)
        return "nsfw"
    success = await post_to_telegram(bot, sub)
    if success:
        posted_ids.add(sub.id)
        save_posted_ids(posted_ids)
        logger.info("Saved post %s to history.", sub.id)
    return "posted" if success else "failed"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main() -> None:
    for var, name in [
        (TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN"),
        (TELEGRAM_CHAT_ID, "TELEGRAM_CHAT_ID"),
        (REDDIT_CLIENT_ID, "REDDIT_CLIENT_ID"),
        (REDDIT_CLIENT_SECRET, "REDDIT_CLIENT_SECRET"),
    ]:
        if not var:
            raise ValueError(f"{name} is not set in .env")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    reddit = make_reddit()

    info = await bot.get_me()
    logger.info("Bot started: @%s — posting every %d minutes", info.username, POST_INTERVAL // 60)

    try:
        while True:
            try:
                posted_ids = load_posted_ids()
                posts = await fetch_top_posts(reddit)

                if not posts:
                    logger.warning("No posts returned from Reddit. Will retry next interval.")
                else:
                    for p in posts:
                        result = await handle_submission(bot, p, posted_ids)
                        if result in ("posted", "failed", "already_posted"):
                            break
                        # removed/nsfw: already saved to posted_ids, keep iterating

                    if all(p.id in posted_ids for p in posts):
                        logger.info("All top posts for today already handled. Waiting for next interval.")

            except asyncprawcore.exceptions.PrawcoreException as e:
                logger.error("Reddit API error: %s", e)
            except asyncpraw.exceptions.AsyncPRAWException as e:
                logger.error("Reddit PRAW error: %s", e)
            except Exception as e:
                logger.error("Unexpected error: %s\n%s", e, traceback.format_exc())

            logger.info("Sleeping %d minutes until next post...", POST_INTERVAL // 60)
            await asyncio.sleep(POST_INTERVAL)
    finally:
        await reddit.close()


if __name__ == "__main__":
    asyncio.run(main())
