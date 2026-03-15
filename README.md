# reddit_to_telegram

Telegram bot that reposts top daily posts from any subreddit, posting at fixed :00 and :30 wall-clock slots.

Supports text, image, GIF, video, and gallery posts. Automatically skips removed, NSFW, and already-posted content. Applies spoiler blur and `<tg-spoiler>` markup for posts tagged as spoilers.

---

## Features

- Reposts top-of-day posts from any subreddit at fixed :00 and :30 wall-clock slots (no drift)
- Includes post body text in media captions; overflows into follow-up messages if too long
- Splits long text posts across multiple messages
- Downloads Reddit videos with audio (fetches separate DASH streams and merges via ffmpeg, no yt-dlp)
- Skips already-posted, removed, NSFW, and low-score posts — tries the next post rather than sleeping
- Applies Telegram spoiler effect on spoiler-tagged posts
- Falls back to a link message if media upload fails
- Persists post history in `posted_ids.json` across restarts

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Python 3.11+ | |
| [uv](https://docs.astral.sh/uv/) | package + venv manager |
| ffmpeg | required for merging Reddit video + audio streams |

---

## 1. Get credentials

### Telegram bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts
2. Copy the **bot token** (looks like `123456789:AABBccdd...`)
3. Add the bot as an **admin** to your channel
4. Get the **chat ID** of your channel:
   - Public channel: use `@your_channel_name`
   - Private channel: forward a message to [@userinfobot](https://t.me/userinfobot) to get the numeric ID

### Reddit API

1. Go to <https://www.reddit.com/prefs/apps> → **create another app**
2. Choose type: **script**
3. Name: anything (e.g. `my-telegram-bot`)
4. Redirect URI: `http://localhost:8080` (unused, but required)
5. Copy **client ID** (under the app name) and **client secret**

---

## 2. Local setup

```bash
git clone https://github.com/your-username/reddit_to_telegram.git
cd reddit_to_telegram

uv sync
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=@your_channel

REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret

# Personal overrides — take priority over config.toml defaults
SUBREDDIT=your_subreddit
TELEGRAM_CHANNEL=@your_channel
```

Shared non-secret settings live in `config.toml`. The `SUBREDDIT` and `TELEGRAM_CHANNEL` env vars let you override the defaults without modifying that file (useful to keep `config.toml` clean in git).

Run:

```bash
uv run main.py
```

---

## 3. Test a specific post

Set `POST_ID` in `test_post.py` to any Reddit post ID, then:

```bash
uv run test_post.py
```

Runs the post through the exact same pipeline as the main bot (filter → post → save history).

---

## 4. Configuration

**`config.toml`** — shared settings (safe to commit):

| Key | Default | Description |
|-----|---------|-------------|
| `reddit.subreddit` | `"pics"` | Subreddit to repost from (without `r/`). Override with `SUBREDDIT` env var. |
| `reddit.fetch_limit` | `100` | How many top-of-day posts to pull per cycle |
| `reddit.min_score` | `1` | Skip posts below this score |
| `reddit.user_agent` | `"telegram-reddit-bot/1.0"` | Reddit API user agent |
| `telegram.channel` | `"@your_channel"` | Footer shown at the bottom of every post. Override with `TELEGRAM_CHANNEL` env var. |
| `bot.history_ttl_hours` | `48` | Purge post IDs older than this (Reddit top-day posts expire in ~24h) |
| `bot.posted_ids_file` | `"posted_ids.json"` | Path to post history file |
| `video.max_duration_minutes` | `5` | Skip videos longer than this |
| `video.download_timeout_minutes` | `3` | Abort download if it takes longer than this |
| `video.max_file_size_mb` | `50` | Telegram bot upload limit |

**`.env`** — secrets and personal overrides (never commit):

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Channel username or numeric ID |
| `REDDIT_CLIENT_ID` | Reddit app client ID |
| `REDDIT_CLIENT_SECRET` | Reddit app client secret |
| `SUBREDDIT` | Overrides `reddit.subreddit` in config.toml |
| `TELEGRAM_CHANNEL` | Overrides `telegram.channel` in config.toml |

---

## 5. Deploy on a Linux server

### Install system dependencies

```bash
sudo apt update && sudo apt install -y ffmpeg curl

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### Clone and configure

```bash
cd /opt
sudo git clone https://github.com/your-username/reddit_to_telegram.git
sudo chown -R $USER:$USER /opt/reddit_to_telegram
cd /opt/reddit_to_telegram

uv sync
cp .env.example .env
nano .env   # fill in your credentials
```

### Create a systemd service

```bash
sudo nano /etc/systemd/system/reddit_to_telegram.service
```

```ini
[Unit]
Description=Reddit to Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/opt/reddit_to_telegram
ExecStart=/root/.local/bin/uv run main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> Replace `YOUR_LINUX_USER` with the owner of `/opt/reddit_to_telegram`. Adjust the `uv` path if needed (`which uv`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable reddit_to_telegram
sudo systemctl start reddit_to_telegram
```

### Useful commands

```bash
sudo systemctl status reddit_to_telegram
sudo journalctl -u reddit_to_telegram -f
sudo systemctl restart reddit_to_telegram
sudo systemctl stop reddit_to_telegram
```

### Update the bot

```bash
cd /opt/reddit_to_telegram
git pull
uv sync
sudo systemctl restart reddit_to_telegram
```

`posted_ids.json` and `.env` are never touched by git.

### Remove completely

```bash
sudo systemctl stop reddit_to_telegram
sudo systemctl disable reddit_to_telegram
sudo rm /etc/systemd/system/reddit_to_telegram.service
sudo systemctl daemon-reload
sudo rm -rf /opt/reddit_to_telegram
```

---

## File structure

```
reddit_to_telegram/
├── main.py            # bot logic
├── test_post.py       # one-shot test by post ID
├── test_caption.py    # test caption splitting with a local image
├── config.toml        # shared settings (subreddit, limits)
├── pyproject.toml     # dependencies (managed by uv)
├── .env               # secrets + personal overrides (never commit)
├── .env.example       # template for .env
└── posted_ids.json    # auto-created, tracks posted history
```
