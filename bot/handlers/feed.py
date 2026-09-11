"""Feed scan — scrapes the LinkedIn feed and asks Claude Code to judge which
new posts are genuinely worth the user's attention.

Posts Claude flags as relevant are triaged one at a time through a single
card — author, age, reactions, an excerpt, Claude's relevance score and
reason, and a position indicator — that gets edited in place as the user
works through the stack with Comment / Keep / Skip / Not relevant actions.
This same browser is shared with the "found posts" list (see `found.py`).

Clicking "Comment" opens a separate AI-driven comment drafting flow: Claude
generates 3 variants (nuance, experience, question), the user can adjust or
regenerate, then marks it as posted.
"""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config import get_settings
from app.scraper import AuthenticationError, LinkedInScraper
from bot.auth import restricted
from bot.comment import generate_comment_variants
from bot.formatting import format_age
from bot.relevance import evaluate_posts
from bot.storage import get_posts_store, get_settings_store

logger = logging.getLogger(__name__)

_posts_store = get_posts_store()
_settings_store = get_settings_store()
_scrape_lock = asyncio.Lock()

TEXT_PREVIEW_LEN = 220
_SEPARATOR = "─" * 24

# Posts per evaluate_posts() call. A single oversized batch risks blowing
# past CLAUDE_TIMEOUT (measured ~1.1s/post) and, worse, an all-or-nothing
# failure used to leave every candidate unnotified — re-queued on top of
# whatever's new next scan, an ever-growing backlog that made subsequent
# scans fail too. Batching bounds each call and lets progress made on
# earlier batches survive a later one failing (see cb_feed_scan).
EVAL_BATCH_SIZE = 25

SCAN_BUTTON = InlineKeyboardButton("🔍 New posts", callback_data="feed:scan")
_CONFIG_BUTTON = InlineKeyboardButton("⚙️ Config", callback_data="config:show")
_SCAN_AGAIN_KEYBOARD = InlineKeyboardMarkup([[SCAN_BUTTON]])
_NEEDS_INTERESTS_KEYBOARD = InlineKeyboardMarkup([[_CONFIG_BUTTON]])

# ctx.user_data flag set while waiting for a comment adjustment instruction
AWAITING_COMMENT_ADJUST = "awaiting_comment_adjust"

_BROWSE_ACTIONS = {
    "keep": ("🔖 Kept for later", lambda store, urn: store.keep(urn)),
    "skip": ("⏭️ Skipped", None),
    "notrelevant": ("🗑️ Dismissed — won't show again", lambda store, urn: store.ignore(urn)),
}


def _sync_scrape(on_progress) -> list[dict]:
    """Run the scraper synchronously — call via a thread executor."""
    posts = LinkedInScraper(get_settings()).scrape(on_progress=on_progress)
    return [p.model_dump(mode="json") for p in posts]


def _make_progress_reporter(query, loop: asyncio.AbstractEventLoop):
    """Build the `on_progress` callback passed into LinkedInScraper.scrape().

    The scraper runs in a worker thread (via run_in_executor), but editing a
    Telegram message must happen on the event loop — so this bridges the two
    with `run_coroutine_threadsafe` rather than awaiting directly. Errors
    from `edit_message_text` (rate limits, "message not modified" on an
    identical scroll count) are swallowed: a missed progress update isn't
    worth failing the scan over.
    """
    async def _push(text: str) -> None:
        try:
            await query.edit_message_text(f"⏳ Scanning your feed…\n\n{text}")
        except Exception:
            pass

    def _on_progress(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_push(text), loop)

    return _on_progress


def _format_card(post: dict, index: int, total: int) -> str:
    author = post.get("author", "?")
    text = post.get("text", "")[:TEXT_PREVIEW_LEN].replace("\n", " ").strip()
    age = format_age(post.get("posted_at") or post.get("first_seen_at", ""))
    reactions = post.get("reactions", 0)
    score = post.get("score")
    reason = post.get("reason", "")

    lines = [f"👤 *{author}*", f"🕐 _{age}_ · 👍 {reactions}"]
    lines += ["", f'"{text}…"']

    if score is not None:
        verdict = f"🎯 *Score {score}*"
        if reason:
            verdict += f" — _{reason}_"
        lines += ["", verdict]
    elif reason:
        lines += ["", f"🤖 _{reason}_"]

    lines += ["", _SEPARATOR, f"📊 {index + 1}/{total}"]
    return "\n".join(lines)


def _post_link(post: dict) -> tuple[str, str]:
    """Best available link for a post: the exact permalink when the scraper
    managed to resolve one, otherwise the author's profile (always available,
    see app/scraper.py's `_resolve_post_url` vs `author_url`). Returns
    `(label, url)`, or `("", "")` when neither is available."""
    if post.get("url"):
        return "🔗 Open on LinkedIn", post["url"]
    if post.get("author_url"):
        return "🔗 Open author's profile", post["author_url"]
    return "", ""


def _browse_keyboard(post: dict) -> InlineKeyboardMarkup:
    """Triage actions attached to a browse card — shared by the scan results
    and the "found posts" browser so both stay visually consistent. The link
    row comes first — reading the actual post is usually the first thing
    needed before deciding what to do with it."""
    rows = []
    label, url = _post_link(post)
    if url:
        rows.append([InlineKeyboardButton(label, url=url)])
    rows.append([
        InlineKeyboardButton("💬 Comment", callback_data="browse:comment"),
        InlineKeyboardButton("🔖 Keep", callback_data="browse:keep"),
        InlineKeyboardButton("⏭️ Skip", callback_data="browse:skip"),
        InlineKeyboardButton("🗑️ Dismiss", callback_data="browse:notrelevant"),
    ])
    return InlineKeyboardMarkup(rows)


async def _start_browse(
    query,
    ctx: ContextTypes.DEFAULT_TYPE,
    posts: list[dict],
    *,
    done_text: str,
    done_keyboard: InlineKeyboardMarkup,
) -> None:
    """Seed a one-card-at-a-time triage session over `posts` (must be
    non-empty — callers handle the empty case themselves) and show the first
    card. The session lives in `chat_data` and is edited in place as the user
    triages each post with the buttons from `_browse_keyboard`."""
    ctx.chat_data["browse"] = {
        "posts": posts,
        "index": 0,
        "done_text": done_text,
        "done_keyboard": done_keyboard,
    }
    await _render_browse_card(query, ctx)


async def _render_browse_card(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the card at the session's current index, or the "done" message
    once every post has been triaged."""
    state = ctx.chat_data.get("browse")
    if not state:
        return

    posts, index = state["posts"], state["index"]
    if index >= len(posts):
        done_text, done_keyboard = state["done_text"], state["done_keyboard"]
        ctx.chat_data.pop("browse", None)
        await query.edit_message_text(done_text, reply_markup=done_keyboard)
        return

    post = posts[index]
    await query.edit_message_text(
        _format_card(post, index, len(posts)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_browse_keyboard(post),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Comment drafting flow helpers
# ---------------------------------------------------------------------------

def _format_variants_message(post: dict, variants: list[dict]) -> str:
    author = html.escape(post.get("author", "?"))
    lines = [f"💬 <b>Post by {author}</b> — 3 angles:", ""]
    for v in variants:
        label = html.escape(v["label"])
        text = html.escape(v["text"])
        lines += [f"<b>{label}</b>", f'"{text}"', ""]
    return "\n".join(lines).rstrip()


def _variants_keyboard(post: dict) -> InlineKeyboardMarkup:
    """Link row first, same as _browse_keyboard — without it, checking the
    actual post from here meant backing all the way out of the comment flow."""
    rows = []
    label, url = _post_link(post)
    if url:
        rows.append([InlineKeyboardButton(label, url=url)])
    rows += [
        [
            InlineKeyboardButton("① Use", callback_data="comment:use:0"),
            InlineKeyboardButton("② Use", callback_data="comment:use:1"),
            InlineKeyboardButton("③ Use", callback_data="comment:use:2"),
        ],
        [
            InlineKeyboardButton("✏️ Adjust", callback_data="comment:adjust"),
            InlineKeyboardButton("🔄 Regenerate", callback_data="comment:regen"),
            InlineKeyboardButton("⬅️ Back", callback_data="comment:back"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _format_ready_message(comment_text: str) -> str:
    escaped = html.escape(comment_text)
    return f"✅ <b>Comment ready — tap to copy:</b>\n\n<code>{escaped}</code>"


def _ready_keyboard(post: dict) -> InlineKeyboardMarkup:
    rows = []
    if post.get("url"):
        rows.append([InlineKeyboardButton("🔗 Open post to paste", url=post["url"])])
    elif post.get("author_url"):
        # Not the exact post (no resolved permalink), so don't imply pasting
        # a comment there works — this just gets the user to the right person.
        rows.append([InlineKeyboardButton("🔗 Open author's profile", url=post["author_url"])])
    rows.append([
        InlineKeyboardButton("✔️ I posted", callback_data="comment:posted"),
        InlineKeyboardButton("⏰ Remind in 1h", callback_data="comment:remind"),
    ])
    rows.append([InlineKeyboardButton("⬅️ Back to angles", callback_data="comment:back_variants")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Feed scan
# ---------------------------------------------------------------------------

@restricted
async def cb_feed_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    interests = _settings_store.get_interests()
    if not interests:
        await query.edit_message_text(
            "⚠️ No topics configured yet — Decker can't tell which posts are worth "
            "your time without knowing what you care about.\n\n"
            "Set them up with ⚙️ Config first.",
            reply_markup=_NEEDS_INTERESTS_KEYBOARD,
        )
        return

    if _scrape_lock.locked():
        await query.message.reply_text("⏳ A scan is already running — hang tight.")
        return

    await query.edit_message_text("⏳ Scanning your feed…")
    loop = asyncio.get_event_loop()
    on_progress = _make_progress_reporter(query, loop)

    async with _scrape_lock:
        try:
            scraped = await loop.run_in_executor(None, _sync_scrape, on_progress)
        except AuthenticationError:
            await query.edit_message_text(
                "❌ LinkedIn rejected the session — your cookies are likely expired.\n"
                "Renew `cookies.json` and try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        except Exception as exc:
            logger.exception("Feed scan failed")
            await query.edit_message_text(f"❌ Scan failed: {exc}")
            return

    _posts_store.record_scrape(scraped)
    candidates = _posts_store.get_unnotified()

    if not candidates:
        await query.edit_message_text(
            "✅ Scan complete — nothing new in your feed since last time.",
            reply_markup=_SCAN_AGAIN_KEYBOARD,
        )
        return

    relevant, evaluated_urns, failed_batches = [], [], 0
    batches = [candidates[i:i + EVAL_BATCH_SIZE] for i in range(0, len(candidates), EVAL_BATCH_SIZE)]

    for i, batch in enumerate(batches):
        if len(batches) > 1:
            await query.edit_message_text(
                f"🤖 Asking Claude to judge new posts… ({i * EVAL_BATCH_SIZE + len(batch)}/{len(candidates)})"
            )
        else:
            await query.edit_message_text(f"🤖 Asking Claude to judge {len(batch)} new post(s)…")

        verdicts = await loop.run_in_executor(None, evaluate_posts, batch, interests)
        if not verdicts:
            failed_batches += 1
            logger.warning("Claude evaluation failed for a batch of %d post(s) — will retry next scan", len(batch))
            continue  # these posts stay unnotified and get retried on the next scan

        # Mark this batch notified right away — a later batch failing must
        # not wipe out progress on posts already evaluated (see feed.py
        # history: this used to be all-or-nothing per scan, which let a
        # single slow/oversized batch make an ever-growing backlog of
        # unnotified posts fail every subsequent scan too).
        batch_urns = []
        for post in batch:
            verdict = verdicts.get(post["urn"])
            if verdict is None:
                continue
            batch_urns.append(post["urn"])
            _posts_store.set_relevance(post["urn"], verdict["relevant"], verdict["reason"], verdict.get("score"))
            if verdict["relevant"]:
                post["score"] = verdict.get("score")
                post["reason"] = verdict["reason"]
                relevant.append(post)
        evaluated_urns.extend(batch_urns)
        _posts_store.mark_notified(batch_urns)

    if not evaluated_urns:
        await query.edit_message_text(
            "⚠️ Couldn't get Claude's evaluation this time — the new posts are still "
            "pending and will be retried on the next scan.",
            reply_markup=_SCAN_AGAIN_KEYBOARD,
        )
        return

    failed_note = f" ({failed_batches} batch(es) failed, will retry next scan)" if failed_batches else ""
    if not relevant:
        await query.edit_message_text(
            f"✅ Scan complete — Claude reviewed {len(evaluated_urns)} new post(s){failed_note}, "
            f"none worth your time right now.",
            reply_markup=_SCAN_AGAIN_KEYBOARD,
        )
    else:
        await _start_browse(
            query,
            ctx,
            relevant,
            done_text=f"✅ Reviewed all {len(relevant)} post(s) worth a look{failed_note}.",
            done_keyboard=_SCAN_AGAIN_KEYBOARD,
        )


# ---------------------------------------------------------------------------
# Browse triage (keep / skip / not relevant)
# ---------------------------------------------------------------------------

@restricted
async def cb_browse_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apply a triage decision to the post currently shown in the browse
    session, then advance to the next card (or the "done" message)."""
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    toast, mutate = _BROWSE_ACTIONS[action]

    state = ctx.chat_data.get("browse")
    if not state or state["index"] >= len(state["posts"]):
        await query.answer()
        return

    if mutate:
        mutate(_posts_store, state["posts"][state["index"]]["urn"])
    await query.answer(toast)

    state["index"] += 1
    await _render_browse_card(query, ctx)


# ---------------------------------------------------------------------------
# Comment drafting handlers
# ---------------------------------------------------------------------------

@restricted
async def cb_comment_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Triggered by browse:comment — generates 3 comment angles via Claude."""
    query = update.callback_query
    await query.answer()

    state = ctx.chat_data.get("browse")
    if not state or state["index"] >= len(state["posts"]):
        return

    post = state["posts"][state["index"]]
    interests = _settings_store.get_interests()

    await query.edit_message_text("💭 Generating comment angles… (20–40s)")

    loop = asyncio.get_event_loop()
    variants = await loop.run_in_executor(
        None,
        lambda: generate_comment_variants(post, interests),
    )

    if not variants:
        await query.edit_message_text(
            "❌ Could not generate comments — try again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="comment:back"),
            ]]),
        )
        return

    ctx.chat_data["comment"] = {"post": post, "variants": variants}
    await query.edit_message_text(
        _format_variants_message(post, variants),
        parse_mode=ParseMode.HTML,
        reply_markup=_variants_keyboard(post),
        disable_web_page_preview=True,
    )


@restricted
async def cb_comment_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the selected variant ready to paste, with posting actions."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":")[-1])
    comment_state = ctx.chat_data.get("comment")
    if not comment_state or idx >= len(comment_state["variants"]):
        return

    comment_state["selected_idx"] = idx
    selected = comment_state["variants"][idx]
    post = comment_state["post"]

    await query.edit_message_text(
        _format_ready_message(selected["text"]),
        parse_mode=ParseMode.HTML,
        reply_markup=_ready_keyboard(post),
        disable_web_page_preview=True,
    )


@restricted
async def cb_comment_regen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Regenerate 3 fresh variants (same post, different wording)."""
    query = update.callback_query
    await query.answer()

    comment_state = ctx.chat_data.get("comment")
    if not comment_state:
        return

    post = comment_state["post"]
    interests = _settings_store.get_interests()

    await query.edit_message_text("🔄 Regenerating… (20–40s)")

    loop = asyncio.get_event_loop()
    variants = await loop.run_in_executor(
        None,
        lambda: generate_comment_variants(post, interests),
    )

    if not variants:
        await query.edit_message_text(
            "❌ Could not regenerate — try again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="comment:back"),
            ]]),
        )
        return

    comment_state["variants"] = variants
    await query.edit_message_text(
        _format_variants_message(post, variants),
        parse_mode=ParseMode.HTML,
        reply_markup=_variants_keyboard(post),
        disable_web_page_preview=True,
    )


@restricted
async def cb_comment_adjust(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Prompt the user to type a short adjustment instruction."""
    query = update.callback_query
    await query.answer()

    ctx.user_data[AWAITING_COMMENT_ADJUST] = True
    await query.edit_message_text(
        "✏️ <b>How should I adjust?</b>\n\n"
        "Type a short instruction, e.g.:\n"
        "• <i>shorter</i>\n"
        "• <i>less formal</i>\n"
        "• <i>add my experience with pytest</i>\n"
        "• <i>more direct, no fluff</i>",
        parse_mode=ParseMode.HTML,
    )


async def receive_comment_adjust(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text receiver for adjustment instructions — called by the combined
    dispatcher in handlers/__init__.py when AWAITING_COMMENT_ADJUST is set."""
    if not ctx.user_data.pop(AWAITING_COMMENT_ADJUST, False):
        return

    instruction = update.message.text.strip()
    comment_state = ctx.chat_data.get("comment")
    if not comment_state:
        return

    post = comment_state["post"]
    previous = comment_state.get("variants", [])
    interests = _settings_store.get_interests()

    await update.message.reply_text("⏳ Adjusting…")

    loop = asyncio.get_event_loop()
    variants = await loop.run_in_executor(
        None,
        lambda: generate_comment_variants(
            post,
            interests,
            instruction=instruction,
            previous_variants=previous,
        ),
    )

    if not variants:
        await update.message.reply_text(
            "❌ Could not adjust — try again or /start to restart."
        )
        return

    comment_state["variants"] = variants
    await update.message.reply_text(
        _format_variants_message(post, variants),
        parse_mode=ParseMode.HTML,
        reply_markup=_variants_keyboard(post),
        disable_web_page_preview=True,
    )


@restricted
async def cb_comment_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Discard the comment draft and return to the browse card."""
    query = update.callback_query
    await query.answer()
    ctx.chat_data.pop("comment", None)
    await _render_browse_card(query, ctx)


@restricted
async def cb_comment_back_variants(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Return from the ready screen back to the 3-variants screen."""
    query = update.callback_query
    await query.answer()

    comment_state = ctx.chat_data.get("comment")
    if not comment_state:
        await _render_browse_card(query, ctx)
        return

    await query.edit_message_text(
        _format_variants_message(comment_state["post"], comment_state["variants"]),
        parse_mode=ParseMode.HTML,
        reply_markup=_variants_keyboard(comment_state["post"]),
        disable_web_page_preview=True,
    )


@restricted
async def cb_comment_posted(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark the post as commented and advance the browse session."""
    query = update.callback_query
    await query.answer("✅ Posted!")

    comment_state = ctx.chat_data.pop("comment", None)
    if comment_state:
        _posts_store.mark_commented(comment_state["post"]["urn"])

    browse_state = ctx.chat_data.get("browse")
    if browse_state:
        browse_state["index"] += 1

    await _render_browse_card(query, ctx)


@restricted
async def cb_comment_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Schedule a 1-hour reminder then advance the browse session."""
    query = update.callback_query
    await query.answer("⏰ Reminder set for 1 hour!")

    comment_state = ctx.chat_data.get("comment")
    if comment_state:
        post = comment_state["post"]
        idx = comment_state.get("selected_idx", 0)
        variants = comment_state.get("variants", [])
        selected_text = variants[idx]["text"] if idx < len(variants) else ""
        chat_id = query.message.chat_id

        async def _remind(context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = f"⏰ <b>Reminder: post your comment!</b>\n\n<code>{html.escape(selected_text)}</code>"
            link_label, link_url = _post_link(post)
            if link_url:
                msg += f'\n\n<a href="{html.escape(link_url)}">{link_label} →</a>'
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

        ctx.application.job_queue.run_once(_remind, when=3600, chat_id=chat_id)

    ctx.chat_data.pop("comment", None)
    browse_state = ctx.chat_data.get("browse")
    if browse_state:
        browse_state["index"] += 1
    await _render_browse_card(query, ctx)
