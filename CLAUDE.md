# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

**All outputs must be in English** — code, comments, docstrings, commit messages, and conversational responses. No French or other languages.

## Overview

One scraper core (`app/`) is exposed through three independent entry points that share this repo but do **not** depend on each other at runtime — each is its own process, started separately, and none of them talk to each other over HTTP or otherwise:

1. **Decker** — Telegram bot (`bot/`, run via `python -m bot.main`) that imports the scraper core (`app/scraper.py`, `app/config.py`) directly, triggers scrapes, sends new posts to Claude Code (subprocess `claude -p`) for LLM-based relevance evaluation, and lets the user triage them one card at a time (Comment / Keep / Skip / Not relevant). This is the only process needed for full end-user functionality — it does not require the REST API or MCP server to be running.
2. **Scraper API** (`main.py`/`app/api.py`) — FastAPI REST service that scrapes the LinkedIn feed, scores posts by keyword relevance, and persists them to `posts.json`. Authentication via session cookies only. Scraping uses `StealthyFetcher` (Scrapling + Patchright/Chromium headless). Optional — for HTTP/curl/Swagger access only.
3. **MCP server** (`mcp_server.py`) — exposes the same scraper core as tools for Claude Code itself. Spawned automatically by Claude Code via `.mcp.json`; never launched manually.

## Stack

- Python 3.12+
- FastAPI + Uvicorn, Pydantic-settings + python-dotenv
- `scrapling[all]>=0.4.8` (StealthyFetcher with Patchright/Chromium)
- `mcp>=2.0.0` (MCPServer — MCP server for Claude Code integration)
- `python-telegram-bot[job-queue]>=20.0` (async, long-polling; the `job-queue` extra pulls in APScheduler — required for the bot's "Remind in 1h" comment reminder, see `bot/handlers/feed.py`)
- `pytest>=8.0.0` (integration smoke tests)
- No Docker

## Commands

```bash
# Setup (run once) — python3 is required here since .venv doesn't exist yet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
patchright install chromium          # required — without this, scrapes return 0 posts silently

# Telegram bot (Decker) — the only process most workflows need; two equivalent entry points:
.venv/bin/python -m bot.main         # canonical form
.venv/bin/python telegram_bot.py     # thin shim, delegates to bot.main
tail -f logs/bot.log                 # watch Decker's logs (scan progress, errors, ...) live

# Scraper REST API (optional — bot does not need this running)
.venv/bin/python main.py             # listens on http://0.0.0.0:8000
# Swagger UI: http://localhost:8000/docs

# CLI scraper (bypasses the API, one-shot)
.venv/bin/python scrape.py --json
.venv/bin/python scrape.py --attempts 1 --keywords "QA,pytest" --min-score 2 --json

# MCP server (stdio, spawned automatically by Claude Code via .mcp.json — never launched manually)
.venv/bin/python mcp_server.py

# Tests
pytest -q                            # all tests (skipped automatically if cookies.json absent)
pytest tests/test_smoke.py -v -s     # verbose with live logs
```

> Commands above use the venv's interpreter by absolute path (`.venv/bin/python`) rather than a bare `python`. Many systems have no `python` on `PATH` at all — only `python3` — so a bare `python ...` command fails with "command not found" unless the venv is `source .venv/bin/activate`d in that *exact* shell first; that activation doesn't carry over to a separate shell invocation (a new terminal, a script, a cron job, each tool call in an automated session). `.venv/bin/python` always resolves correctly and always uses the venv's installed dependencies, so prefer it — especially for anything non-interactive. **Only run one `bot.main` instance at a time**: a second process polling the same `TELEGRAM_BOT_TOKEN` makes Telegram reject both with `Conflict: terminated by other getUpdates request` until one is killed.

## Project structure

```
main.py              # uvicorn entry point (runs app.api:app)
scrape.py            # standalone CLI scraper (no server needed)
mcp_server.py        # MCP server — exposes scraper as tools for Claude Code (stdio)
telegram_bot.py      # thin shim → delegates to bot.main (either entry point works)

app/                 # Scraper REST API
  api.py             # ALL FastAPI routes + lifespan singletons
  config.py          # Settings (pydantic-settings), save_override(), get_settings()
  cookies.py         # load_cookies() — converts Cookie Editor JSON for Playwright
  models.py          # Pydantic models: Post, ScrapeResult, ScrapeRequest, ...
  scraper.py         # LinkedInScraper, _extract_posts(), AuthenticationError
  scorer.py          # score_post() / score_posts() — keyword-based, pure functions
  storage.py         # PostStorage — JSON file (posts.json) with upsert and dedup

bot/                 # Decker — the Telegram bot
  main.py            # bot entry point: builds Application, registers handlers, runs polling
  config.py          # BOT_TOKEN + AUTHORIZED_USER_ID from env — distinct from app/config.py
  auth.py            # @restricted decorator — silently drops updates from unknown users
  formatting.py      # shared helpers (format_age, etc.) for Telegram message rendering
  relevance.py       # LLM-based scoring: calls `claude -p` as a subprocess to judge posts
                     #   ↳ distinct from app/scorer.py which is pure keyword matching
  comment.py         # LLM-based comment drafting: calls `claude -p` for 3 angled variants
                     #   (generate + regenerate + free-text adjust), same subprocess approach as relevance.py
  handlers/
    __init__.py      # register_handlers() — wires all CommandHandler / CallbackQueryHandler
    start.py         # /start → main menu keyboard
    feed.py          # feed:scan — triggers scrape + Claude eval + browse-card triage flow
    found.py         # found:list — paginated list of Claude-relevant posts
    config.py        # config:show / config:edit + text handler for interests input
  storage/           # bot-side persistence (bot_posts.json, bot_settings.json)
                     #   ↳ distinct from app/storage.py which backs the REST API
    interfaces.py    # SettingsStore + PostsStore ABCs
    json_store.py    # SettingsStore backed by bot_settings.json
    json_posts_store.py  # PostsStore backed by bot_posts.json

tests/
  test_smoke.py      # integration tests: real scrape via cookies.json; auto-skipped if absent
pytest.ini           # pythonpath=. + live log config

.mcp.json            # MCP server registration for Claude Code (absolute paths)
cookies.json         # LinkedIn session cookies (git-ignored, create manually)
posts.json           # scraper-side persisted posts (git-ignored, created automatically)
bot_posts.json       # bot-side persisted posts with triage state (git-ignored)
bot_settings.json    # bot-side interests config (git-ignored)
config_override.json # runtime config overrides (git-ignored, written by PUT /config/interests)
.env                 # copy from .env.example (git-ignored)
logs/bot.log          # Decker's rotating log file (git-ignored, created automatically — see bot/main.py)
```

## Architecture

### Scraper API (`app/`)

`app/api.py` owns four module-level singletons (`_settings`, `_storage`, `_scraper`, `_scrape_lock`) initialized in the FastAPI `lifespan` context.

`POST /scrape` runs `LinkedInScraper.scrape()` in a thread via `loop.run_in_executor` (Patchright is synchronous). `_scrape_lock` prevents concurrent scrapes (returns 409).

**Config layering**: `.env` → `config_override.json` → runtime. `PUT /config/interests` and `update_interests` (MCP) write to `config_override.json` and call `get_settings.cache_clear()`.

**Keyword scoring** (`app/scorer.py`): pure function, each keyword occurrence adds `len(keyword.split())` points. Applied at scrape time and re-applied on every `PUT /config/interests`.

**Post deduplication**: `urn:li:activity:ID` when the comment count renders, otherwise `urn:li:post:hash:HEX` (MD5 of author + text[:200]). `PostStorage.upsert_posts()` upgrades hash URNs to real ones when they arrive.

### MCP server (`mcp_server.py`)

Mirrors the singleton pattern (`_settings`, `_storage`, `_scraper`, `_scrape_lock`) at module level. Uses `threading.Lock` (not asyncio) because `scrape_feed` dispatches via `anyio.to_thread.run_sync`. The other four tools (`get_posts`, `get_interesting_posts`, `update_interests`, `get_config`) are plain `def`. Registered via `.mcp.json`; Claude Code spawns it as a stdio subprocess.

### Decker bot (`bot/`)

Entry: `bot/main.py` builds the `Application`, calls `register_handlers()`, runs long-polling.

**LLM relevance** (`bot/relevance.py`): `evaluate_posts()` calls `claude -p <prompt> --output-format json` as a subprocess. It sends a batch of post excerpts and receives `[{urn, relevant, score, reason}]`. This is intentionally separate from `app/scorer.py` — keyword matching is fast and used by the REST API; LLM evaluation is slow and used only by the bot. Call from a thread executor — it blocks.

**Triage flow** (`bot/handlers/feed.py`): `feed:scan` callback scrapes, runs LLM eval, then presents posts one card at a time. Each card is edited in place; actions are Comment / Keep / Skip / Not relevant. State is persisted immediately via `PostsStore`.

**LLM evaluation is batched** (`EVAL_BATCH_SIZE = 25` posts/call, measured ~1.1s/post): a single oversized `claude -p` call for the whole unnotified backlog used to risk exceeding `CLAUDE_TIMEOUT`, and — because a failed call used to leave every candidate unnotified — a single timeout meant the backlog got re-queued on top of whatever was new next scan, an unbounded growth that made every subsequent scan fail too. Each batch is now marked notified (via `PostsStore.mark_notified`) as soon as it succeeds, so a later batch failing can't undo progress already made; unevaluated posts simply retry on the next scan (see `tests/test_feed_scan_batching.py`).

**Scan progress**: most of a scrape's wall time isn't the scroll/network-idle wait but `LinkedInScraper._resolve_post_url()` resolving a real permalink for every newly-seen post one at a time, since the feed DOM almost never exposes a real `urn:li:activity` id directly (see `app/scraper.py`). It opens each post's "more options" menu, clicks "Copy link to post", and reads the permalink from the confirmation toast's "View post" link (`_TOAST_VIEW_POST_LOCATOR`) — not from the OS clipboard, which doesn't reliably round-trip in headless Chromium (confirmed by live testing: the click lands and the toast renders, but neither `navigator.clipboard.writeText()` nor a native `copy` event ever fires here). The toast is a reused element that does not always swap in its new href synchronously with the click, so `_resolve_post_url` captures the toast's href before clicking and polls for it to actually change afterward, rather than trusting whatever is present immediately — a naive read could (and, before this fix, did — confirmed live) silently return the previous resolved post's link instead of failing. `LinkedInScraper.scrape()` takes an `on_progress: Callable[[str], None]` callback, invoked once after the initial extraction and once per scroll step with the same text as the corresponding log line. `bot/handlers/feed.py::_make_progress_reporter()` bridges it back into the running event loop with `asyncio.run_coroutine_threadsafe` (the scraper itself runs in a thread executor) and live-edits the "⏳ Scanning…" message with each update. Logs (scraper progress, httpx, apscheduler, telegram.ext, ...) go to both stdout and a rotating file at `logs/bot.log` (`bot/main.py::_configure_logging()`) — `tail -f logs/bot.log` to watch a scan in real time; httpx's per-long-poll-request INFO lines are silenced there to keep the scraper's own progress lines visible.

**Comment drafting** (`bot/comment.py`, wired into `bot/handlers/feed.py`): "Comment" triggers `generate_comment_variants()` (another `claude -p` subprocess call, same pattern as `relevance.py`) for 3 angled drafts (nuance / personal experience / question). The user can pick one, regenerate, or send a free-text adjustment instruction. "Remind in 1h" schedules a follow-up message via `ctx.application.job_queue.run_once()` — this requires the `job-queue` extra (APScheduler) to be installed; without it `job_queue` is silently `None` and tapping the button raises `AttributeError`. Covered by `tests/test_comment_remind.py`.

**Storage split**:
- `app/storage.py` / `posts.json` — owned by the REST API and MCP server
- `bot/storage/` / `bot_posts.json` + `bot_settings.json` — owned exclusively by the bot; includes triage state (`notified`, `relevant`, `commented`, `kept`, `ignored`)

**Auth**: `@restricted` in `bot/auth.py` silently drops any update not from `TELEGRAM_USER_ID`.

## Code conventions

- All FastAPI endpoints are `async def`; scraping itself runs in a thread executor
- `StealthyFetcher.fetch()` always called with `headless=True`, `network_idle=True`
- Cookies loaded via `load_cookies()` from `app/cookies.py` — `sameSite` intentionally omitted to avoid Playwright rejecting `sameSite="None"` without `secure=True`
- All configurable values go through `Settings` — never `os.environ` directly (except `bot/config.py` which uses `os.environ` directly for the two bot-only vars)
- `HTTPException` codes: 401 for bad cookies, 409 for concurrent scrape, 503 for LinkedIn unreachable

## Critical rules

- **Never commit** `cookies.json`, `posts.json`, `bot_posts.json`, `bot_settings.json`, `config_override.json`, `.env`
- **Do not replace the scraping engine**: Scrapling/StealthyFetcher is intentional for bypassing LinkedIn anti-bot — do not migrate to requests/httpx/aiohttp
- **Storage stays file-based** (JSON) unless explicitly requested otherwise
- **Do not merge `bot/relevance.py` and `app/scorer.py`** — they serve different purposes (LLM vs keyword), different callers, and different performance profiles
- **MCP server registration belongs in `.mcp.json`**, not in `.claude/settings.json`
- **`.mcp.json` uses absolute paths** — update `command` and `cwd` if the project moves

## Cookie management

The critical cookie is `li_at` (session token). `load_cookies()` warns if it's missing. To renew:
1. Log into LinkedIn in Chrome/Firefox
2. Install **Cookie Editor** extension → Export as JSON
3. Save as `cookies.json` at project root, or upload via `POST /config/cookies`

Expired cookies: logs show `302 → /uas/login`. Valid cookies: `307 → 200` on `/feed/`.

## REST API endpoints

| Method | Route | Notes |
|--------|-------|-------|
| POST | `/scrape` | Body: `{"scroll_attempts": N}` (optional). Returns `ScrapeResult`. |
| GET | `/posts` | Query: `limit`, `offset`, `min_score` |
| GET | `/posts/interesting` | Query: `threshold` (defaults to `relevance_threshold`) |
| GET | `/config` | Full active config |
| PUT | `/config/interests` | Body: `{"keywords": [...], "threshold": N}`. Re-scores all stored posts. |
| POST | `/config/cookies` | Multipart file upload (Cookie Editor JSON format) |
| GET | `/health` | Returns `{"status": "ok", "version": "..."}` |
