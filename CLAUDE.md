# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Install dependencies
pip install -r requirements.txt

# Preview dashboard only (no Telegram token needed)
python preview_server.py

# Full bot + dashboard
python main.py
```

The dashboard runs on port 5000 by default (`http://localhost:5000`).  
Set `NO_AUTH=1` to skip the login screen during local development.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (Railway). Absent → uses JSON files |
| `BOT_TOKEN` | Override token for all active bots at startup |
| `DATA_DIR` | Directory for JSON files and uploads (default: `.`) |
| `PORT` | Web server port (default: `5000`) |
| `SECRET_KEY` | Flask session secret (auto-generated if absent) |
| `ADMIN_PASSWORD` | Initial password for the `admin` user (default: `admin`) |
| `NO_AUTH` | Set to `1` to disable login for local dev |

## Architecture

The app is a single Python process with two concurrent parts:

- **Async event loop** (`asyncio`) runs Telegram bot(s) via `python-telegram-bot` v20 and APScheduler
- **Flask web server** runs in a `daemon=True` thread alongside the async loop

Cross-thread communication from Flask → async loop uses `asyncio.run_coroutine_threadsafe`.

### File layout

| File | Role |
|---|---|
| `main.py` | Entry point. `BotManager` class manages N simultaneous bot instances (each with its own `Application` + `AsyncIOScheduler`). `run()` starts Flask in a thread then awaits forever. |
| `bot.py` | Bot handlers (`/start`, animated emoji registration), `send_scheduled_message()`, `setup_scheduler()`. |
| `web.py` | Flask API + dashboard routes. `set_context()` wires the Flask app to `BotManager` and the event loop. |
| `db.py` | Dual-mode data layer: JSON files (local) or PostgreSQL (Railway). All reads/writes go through this module. |
| `templates/index.html` | Single-page dashboard (vanilla JS + Bootstrap). All API calls go to `/api/*`. |
| `preview_server.py` | Runs only the Flask app for UI development without a live bot. |

### Data layer (`db.py`)

`use_postgres()` checks for `DATABASE_URL`. The same public functions (`load_config`, `save_config`, `load_messages`, `save_messages`, `save_emoji`, etc.) work in both modes. On first startup with PostgreSQL, `migrate_from_json()` imports any existing JSON files automatically.

Config is stored across several PostgreSQL tables: `settings`, `bots`, `groups`, `button_configs`. `load_config()` assembles these into a single dict with the same shape as `config.json`.

### Multi-bot architecture

`BotManager` in `main.py` holds `_apps: dict[bot_id → Application]` and `_schedulers: dict[bot_id → AsyncIOScheduler]`. Each bot has its own independent scheduler. `start_bot(bot_id, token, config)` and `stop_bot(bot_id)` manage lifecycle per-bot under an `asyncio.Lock`.

Token validation (`_is_valid_token`) rejects known placeholder strings and tokens without `:`. Startup attempts tokens in order: (1) per-bot token from DB, (2) global `BOT_TOKEN` env/setting as fallback, (3) a `"default"` bot using the global token if nothing else started.

### Animated emoji system

Custom Telegram emoji IDs are stored in the `emoji_map` table. At send time, `preprocess_animated_emoji()` in `bot.py` wraps matching Unicode characters with `<tg-emoji emoji-id="...">` tags (HTML parse mode only). Users register new emojis by sending them directly to the bot in a private chat — `handle_emoji_registration()` extracts IDs via `msg.parse_entity(entity)` (handles UTF-16 offsets correctly). The COMPRA letter pack (C/O/M/P/R/A) is seeded on every startup via `seed_emoji_defaults()`.

### Scheduled messages

Each message can have multiple schedules. Each schedule targets one group with a cron expression (`day_of_week + hour + minute`, APScheduler `CronTrigger`). `setup_scheduler()` clears and rebuilds all jobs from the current `messages` + `config`. It is called on startup and whenever the dashboard saves a change (`_reload_callback`).

## CI/CD

Push to `master` → GitHub Actions builds a Docker image → pushes to `ghcr.io` → Railway pulls and redeploys. See `.github/workflows/docker.yml`. The `Dockerfile` installs `ffmpeg` (required for video notes) and sets `DATA_DIR=/data`. Railway mounts `/data` as a persistent volume (`railway.toml`).
