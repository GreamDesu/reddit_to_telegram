# reddit_to_telegram

Telegram bot that reposts top daily posts from any subreddit on a configurable schedule.

The target subreddit, posting interval, and all other settings are set in `config.toml`.

Supports text, image, GIF, video, and gallery posts. Automatically skips removed, NSFW, and already-posted content. Applies spoiler blur and `<tg-spoiler>` markup for posts tagged as spoilers.

---

## Features

- Reposts top-of-day posts from any subreddit on a configurable schedule (default: every 30 minutes)
- Splits long text posts across multiple messages
- Downloads Reddit videos (merges audio + video via ffmpeg)
- Skips posts that are removed, NSFW, or already posted
- Applies Telegram spoiler effect on spoiler-tagged posts
- Falls back to a link message if media upload fails
- Persists post history in `posted_ids.json` across restarts

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11+ | |
| [uv](https://docs.astral.sh/uv/) | latest | package + venv manager |
| ffmpeg | any recent | required for Reddit video audio/video merging |

---

## 1. Get credentials

### Telegram bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts
2. Copy the **bot token** (looks like `123456789:AABBccdd...`)
3. Add the bot as an **admin** to your channel
4. Get the **chat ID** of your channel:
   - For a public channel: use `@your_channel_name`
   - For a private channel: forward a message to [@userinfobot](https://t.me/userinfobot) to get the numeric ID

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

# Install dependencies
uv sync

# Copy and fill in credentials
cp .env.example .env
```

Edit `.env` (secrets only):

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=@your_channel

REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
```

All other settings are in `config.toml` — edit to set your subreddit and preferences:

```toml
[reddit]
subreddit = ""          # subreddit name without r/
fetch_limit = 100
user_agent = "telegram-reddit-bot/1.0"

[telegram]
channel = "@your_channel"       # shown at the bottom of every post

[bot]
post_interval_minutes = 30

[video]
max_duration_minutes = 5
download_timeout_minutes = 3
max_file_size_mb = 50
```

Run:

```bash
uv run main.py
```

---

## 3. Test a specific post

Change `POST_ID` in `test_post.py` to any Reddit post ID, then:

```bash
uv run test_post.py
```

This runs the post through the exact same pipeline as the main bot (filter → post → save history).

---

## 4. Configuration

**`config.toml`** — all non-secret settings:

| Key | Default | Description |
|-----|---------|-------------|
| `reddit.subreddit` | `"none"` | Subreddit to repost from (without `r/`) |
| `reddit.fetch_limit` | `100` | How many top-of-day posts to pull per cycle |
| `reddit.user_agent` | `"telegram-reddit-bot/1.0"` | Reddit API user agent string |
| `telegram.channel` | `"@your_channel"` | Channel handle shown at the bottom of every post |
| `bot.post_interval_minutes` | `30` | Minutes between posts |
| `bot.history_ttl_hours` | `48` | Purge post IDs older than this; Reddit top-day posts expire in ~24h so 48h gives a safe buffer |
| `bot.posted_ids_file` | `"posted_ids.json"` | Path to post history file |
| `video.max_duration_minutes` | `5` | Skip videos longer than this |
| `video.download_timeout_minutes` | `3` | Abort download if it takes longer than this |
| `video.max_file_size_mb` | `50` | Telegram bot upload limit |

**`.env`** — secrets only:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Channel username or numeric ID |
| `REDDIT_CLIENT_ID` | Reddit app client ID |
| `REDDIT_CLIENT_SECRET` | Reddit app client secret |

---

## 5. Deploy on a Linux server

### Install system dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg curl

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

Paste:

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
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> Replace `YOUR_LINUX_USER` with the user that owns `/opt/reddit_to_telegram` (e.g. `ubuntu`, `debian`, or `root`).
> Adjust the `uv` path if needed — check with `which uv`.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable reddit_to_telegram
sudo systemctl start reddit_to_telegram
```

### Useful commands

```bash
# Check status
sudo systemctl status reddit_to_telegram

# Follow live logs
sudo journalctl -u reddit_to_telegram -f

# Restart after config change
sudo systemctl restart reddit_to_telegram

# Stop
sudo systemctl stop reddit_to_telegram
```

### Update the bot

```bash
cd /opt/reddit_to_telegram
git pull
uv sync
sudo systemctl restart reddit_to_telegram
```

After pulling, check if `config.toml` has any new keys by comparing with the repo's version — new options won't be added to your local file automatically. `posted_ids.json` and `.env` are never touched by git.

### Remove completely

```bash
# Stop and disable the service
sudo systemctl stop reddit_to_telegram
sudo systemctl disable reddit_to_telegram

# Remove the service file
sudo rm /etc/systemd/system/reddit_to_telegram.service
sudo systemctl daemon-reload

# Remove the project directory (includes .env, posted_ids.json, venv)
sudo rm -rf /opt/reddit_to_telegram
```

---

## File structure

```
reddit_to_telegram/
├── main.py            # bot logic
├── test_post.py       # one-shot test by post ID
├── config.toml        # all settings (subreddit, intervals, limits)
├── pyproject.toml     # dependencies (managed by uv)
├── .env               # secrets: bot token, Reddit API keys (never commit)
├── .env.example       # template for .env
└── posted_ids.json    # auto-created, tracks posted history
```
