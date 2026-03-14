#!/usr/bin/env python3
"""Telegram bot that reposts top daily posts from a subreddit on a schedule."""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.request
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

_SCRIPT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(os.getenv("CONFIG_FILE", _SCRIPT_DIR / "config.toml"))


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
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")

# Settings — env vars take priority over config.toml (use .env for personal overrides)
SUBREDDIT          = os.getenv("SUBREDDIT") or _cfg["reddit"]["subreddit"]
FETCH_LIMIT        = _cfg["reddit"].get("fetch_limit", 100)
REDDIT_USER_AGENT  = _cfg["reddit"].get("user_agent", "telegram-reddit-bot/1.0")
TELEGRAM_CHANNEL   = os.getenv("TELEGRAM_CHANNEL") or _cfg["telegram"]["channel"]
POST_INTERVAL      = int(_cfg["bot"]["post_interval_minutes"]) * 60
POSTED_IDS_FILE    = _SCRIPT_DIR / _cfg["bot"].get("posted_ids_file", "posted_ids.json")
MAX_VIDEO_DURATION = int(_cfg["video"]["max_duration_minutes"]) * 60
DOWNLOAD_TIMEOUT   = int(_cfg["video"]["download_timeout_minutes"]) * 60
MAX_FILE_SIZE      = int(_cfg["video"]["max_file_size_mb"]) * 1024 * 1024
HISTORY_TTL        = int(_cfg["bot"].get("history_ttl_hours", 48)) * 3600
MIN_SCORE          = int(_cfg["reddit"].get("min_score", 1))
MAX_GALLERY_ITEMS  = 10  # Telegram media group limit


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


async def get_access_token(reddit: asyncpraw.Reddit) -> str | None:
    """Return the current OAuth Bearer token from the asyncpraw session."""
    try:
        await reddit._core._authorizer.refresh()
        return reddit._core._authorizer.access_token
    except Exception as e:
        logger.warning("Could not retrieve Reddit access token: %s", e)
        return None


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
    if (
        parent_data.get("removed_by_category")
        or parent_data.get("selftext", "") in ("[removed]", "[deleted]")
    ):
        sub._parent_removed = True
        return sub
    try:
        parent = await reddit.submission(id=parent_id)
        # Propagate NSFW flag: the crosspost or embedded parent data may carry
        # over_18=True even when the fetched parent object doesn't (subreddit mismatch).
        if getattr(sub, "over_18", False) or parent_data.get("over_18"):
            parent.over_18 = True
        parent.title = sub.title
        return parent
    except Exception as e:
        logger.warning("Could not resolve crosspost parent %s: %s", parent_id, e)
        return sub


def is_removed(sub: Submission) -> bool:
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

def build_media_caption(t: str, body: str, footer: str, limit: int = MessageLimit.CAPTION_LENGTH) -> tuple[str, str]:
    """Fit as much of body as possible into a media caption; return (caption, overflow).

    Caption structure: title [+ \\n\\n + body_chunk] + footer
    overflow is the remaining body that didn't fit (empty string if all fitted).
    """
    if not body:
        max_title = limit - len(footer)
        return (t if len(t) <= max_title else t[:max_title - 1] + "…") + footer, ""

    prefix = t + "\n\n"
    max_body = limit - len(prefix) - len(footer)

    if max_body <= 0:
        # Title alone barely fits; drop body from caption entirely
        max_title = limit - len(footer)
        return (t if len(t) <= max_title else t[:max_title - 1] + "…") + footer, body

    if len(body) <= max_body:
        return f"{prefix}{body}{footer}", ""

    # Split body at a word/line boundary
    split_at = body.rfind("\n", 0, max_body)
    if split_at == -1:
        split_at = body.rfind(" ", 0, max_body)
    if split_at == -1:
        split_at = max_body
    return f"{prefix}{body[:split_at].rstrip()}{footer}", body[split_at:].lstrip()


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


def _download_video_sync(url: str, access_token: str | None = None) -> str | None:
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
    if access_token:
        ydl_opts["http_headers"] = {"Authorization": f"Bearer {access_token}"}
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


async def download_video(url: str, access_token: str | None = None) -> str | None:
    """Download video in a thread with a hard timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_download_video_sync, url, access_token),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Video download timed out after %ds: %s", DOWNLOAD_TIMEOUT, url)
        return None


def _get_audio_suffix_from_dash(dash_url: str, headers: dict) -> str | None:
    """Fetch the DASH manifest and return the audio stream filename."""
    try:
        req = urllib.request.Request(dash_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            mpd = resp.read().decode("utf-8")
        # Reddit manifests use either contentType="audio" or mimeType="audio/mp4"
        audio_block = re.search(
            r'(?:contentType="audio"|mimeType="audio/[^"]*").*?<BaseURL>([^<]+)</BaseURL>',
            mpd,
            re.DOTALL,
        )
        if audio_block:
            return audio_block.group(1).strip()
    except Exception as e:
        logger.warning("Failed to fetch/parse DASH manifest %s: %s", dash_url, e)
    return None


def _download_reddit_video_sync(rv: dict, access_token: str | None) -> str | None:
    """Download Reddit-hosted video + audio streams and merge with ffmpeg.

    Reddit stores video and audio as separate DASH streams on v.redd.it CDN.
    This bypasses yt-dlp (which tries to scrape www.reddit.com, blocked on some
    cloud IPs) by fetching the streams directly from URLs in the submission data.
    """
    fallback_url = rv.get("fallback_url", "")
    if not fallback_url:
        return None

    m = re.match(r"(https://v\.redd\.it/[^/?]+)", fallback_url)
    if not m:
        return None
    base = m.group(1)
    video_url = fallback_url.split("?")[0]

    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    tmp_dir = tempfile.mkdtemp()
    try:
        video_path = os.path.join(tmp_dir, "video.mp4")
        req = urllib.request.Request(video_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(video_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        if os.path.getsize(video_path) > MAX_FILE_SIZE:
            logger.warning("Reddit video stream exceeds size limit")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        # Resolve audio filename from the DASH manifest; fall back to known patterns
        dash_url = rv.get("dash_url", "")
        audio_suffix = _get_audio_suffix_from_dash(dash_url, headers) if dash_url else None
        audio_candidates = [audio_suffix] if audio_suffix else ["DASH_audio.mp4", "DASH_AUDIO_128.mp4", "DASH_AUDIO_64.mp4"]

        audio_path = os.path.join(tmp_dir, "audio.mp4")
        audio_downloaded = False
        for suffix in audio_candidates:
            try:
                req = urllib.request.Request(f"{base}/{suffix}", headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    with open(audio_path, "wb") as f:
                        shutil.copyfileobj(resp, f)
                audio_downloaded = True
                logger.info("Downloaded audio stream: %s/%s", base, suffix)
                break
            except Exception:
                continue

        if not audio_downloaded:
            logger.warning("No audio stream found for %s, returning video-only", base)
            return video_path

        merged_path = os.path.join(tmp_dir, "merged.mp4")
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-i", audio_path, "-c", "copy", merged_path],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0 and os.path.exists(merged_path):
                return merged_path
            logger.warning("ffmpeg merge failed (rc=%d), using video-only", result.returncode)
        except FileNotFoundError:
            logger.warning("ffmpeg not found in PATH, using video-only")
        except Exception as e:
            logger.warning("ffmpeg error (%s), using video-only", e)
        return video_path

    except Exception as e:
        logger.warning("Direct Reddit video download failed: %s", e)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


async def download_reddit_video(rv: dict, access_token: str | None) -> str | None:
    """Download Reddit video+audio in a thread with a hard timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_download_reddit_video_sync, rv, access_token),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Reddit video download timed out after %ds", DOWNLOAD_TIMEOUT)
        return None


def get_gallery_urls(sub: Submission) -> list[str]:
    """Extract ordered image URLs from a Reddit gallery post."""
    media_metadata: dict = getattr(sub, "media_metadata", {}) or {}
    gallery_data: dict = getattr(sub, "gallery_data", {}) or {}
    urls = []
    for item in gallery_data.get("items", []):
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

async def post_to_telegram(bot: Bot, sub: Submission, reddit: asyncpraw.Reddit | None = None) -> bool:
    title = sub.title
    url = sub.url
    post_type = get_post_type(sub)
    spoiler = bool(getattr(sub, "spoiler", False))
    short_url = f"https://redd.it/{sub.id}"
    t = html.escape(title)

    footer = f"\n{short_url}\n{TELEGRAM_CHANNEL}"
    body = html.escape((sub.selftext or "").strip())
    if body and spoiler:
        body = f"<tg-spoiler>{body}</tg-spoiler>"

    caption, overflow = build_media_caption(t, body, footer)
    link_text = f"<b>{t}</b>\n{url}{footer}"

    async def send_link() -> None:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=link_text, parse_mode="HTML")

    async def send_body() -> None:
        """Send overflow body text as follow-up message(s) after media."""
        if overflow:
            for part in split_text(overflow):
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=part, parse_mode="HTML")

    logger.info("Posting [%s%s] %s", post_type, " spoiler" if spoiler else "", title[:60])

    try:
        if post_type == "text":
            full = f"<b>{t}</b>\n\n{body}{footer}" if body else f"<b>{t}</b>{footer}"
            for part in split_text(full):
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=part, parse_mode="HTML")

        elif post_type == "image":
            try:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID, photo=url,
                    caption=caption, parse_mode="HTML", has_spoiler=spoiler,
                )
                await send_body()
            except TelegramError as e:
                logger.warning("send_photo failed (%s), sending link instead", e)
                await send_link()

        elif post_type == "gif":
            try:
                await bot.send_animation(
                    chat_id=TELEGRAM_CHAT_ID, animation=url,
                    caption=caption, parse_mode="HTML", has_spoiler=spoiler,
                )
                await send_body()
            except TelegramError as e:
                logger.warning("send_animation failed (%s), sending link instead", e)
                await send_link()

        elif post_type == "video":
            access_token = await get_access_token(reddit) if reddit else None
            if "v.redd.it" in url:
                rv = (getattr(sub, "media", None) or {}).get("reddit_video", {})
                video_path = await download_reddit_video(rv, access_token)
            else:
                video_path = await download_video(url, access_token)

            if video_path:
                try:
                    with open(video_path, "rb") as vf:
                        await bot.send_video(
                            chat_id=TELEGRAM_CHAT_ID, video=vf,
                            caption=caption, parse_mode="HTML",
                            supports_streaming=True, has_spoiler=spoiler,
                        )
                    await send_body()
                except TimedOut:
                    logger.warning("send_video timed out; assuming it went through")
                    await send_body()
                except TelegramError as e:
                    logger.warning("send_video failed (%s), sending link instead", e)
                    await send_link()
                finally:
                    shutil.rmtree(os.path.dirname(video_path), ignore_errors=True)
            else:
                # Download failed — try letting Telegram fetch the fallback URL directly
                # (last resort: no audio, but better than a bare link)
                fallback_mp4 = (
                    (getattr(sub, "media", None) or {}).get("reddit_video", {}).get("fallback_url")
                    if "v.redd.it" in url else None
                )
                if fallback_mp4:
                    try:
                        logger.info("Trying Telegram URL video send for %s", sub.id)
                        await bot.send_video(
                            chat_id=TELEGRAM_CHAT_ID, video=fallback_mp4,
                            caption=caption, parse_mode="HTML",
                            supports_streaming=True, has_spoiler=spoiler,
                            read_timeout=60, write_timeout=60,
                        )
                        await send_body()
                    except TimedOut:
                        logger.warning("Telegram URL video send timed out; assuming it went through")
                        await send_body()
                    except TelegramError as e:
                        logger.warning("Telegram URL video send failed (%s), sending link", e)
                        await send_link()
                else:
                    await send_link()

        elif post_type == "gallery":
            image_urls = get_gallery_urls(sub)
            if not image_urls:
                await send_link()
            else:
                batch = image_urls[:MAX_GALLERY_ITEMS]
                media = [
                    InputMediaPhoto(media=batch[0], caption=caption, parse_mode="HTML", has_spoiler=spoiler),
                    *[InputMediaPhoto(media=u, has_spoiler=spoiler) for u in batch[1:]],
                ]
                try:
                    await bot.send_media_group(
                        chat_id=TELEGRAM_CHAT_ID, media=media,
                        read_timeout=60, write_timeout=60,
                    )
                    await send_body()
                except TimedOut:
                    logger.warning("send_media_group timed out; assuming it went through")
                    await send_body()
                except TelegramError as e:
                    logger.warning("send_media_group failed (%s), sending link", e)
                    await send_link()

        else:  # link
            await send_link()

        return True

    except Exception as e:
        logger.error("Failed to post %s: %s", sub.id, e)
        return False


async def handle_submission(bot: Bot, sub: Submission, posted_ids: set[str], reddit: asyncpraw.Reddit | None = None) -> str:
    """Run one submission through the full filter + post pipeline.

    Mutates posted_ids and persists it for skip/post outcomes.
    Returns: 'posted' | 'failed' | 'removed' | 'nsfw' | 'already_posted'
    """
    if sub.id in posted_ids:
        return "already_posted"

    if sub.score < MIN_SCORE:
        logger.info("Skipping low-score post %s (score=%d): %s", sub.id, sub.score, sub.title[:60])
        return "low_score"

    if is_removed(sub):
        skip_reason = "removed"
    elif is_nsfw(sub):
        skip_reason = "nsfw"
    else:
        skip_reason = None

    if skip_reason:
        logger.info("Skipping %s post %s: %s", skip_reason, sub.id, sub.title[:60])
        posted_ids.add(sub.id)
        save_posted_ids(posted_ids)
        return skip_reason

    success = await post_to_telegram(bot, sub, reddit)
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
                        result = await handle_submission(bot, p, posted_ids, reddit)
                        if result in ("posted", "failed", "already_posted"):
                            break
                        # removed/nsfw/low_score: keep iterating to find a postable submission

                    if all(p.id in posted_ids for p in posts):
                        logger.info("All top posts for today already handled. Waiting for next interval.")

            except (asyncprawcore.exceptions.PrawcoreException, asyncpraw.exceptions.AsyncPRAWException) as e:
                logger.error("Reddit API error: %s", e)
            except Exception as e:
                logger.error("Unexpected error: %s", e, exc_info=True)

            logger.info("Sleeping %d minutes until next post...", POST_INTERVAL // 60)
            await asyncio.sleep(POST_INTERVAL)
    finally:
        await reddit.close()


if __name__ == "__main__":
    asyncio.run(main())
