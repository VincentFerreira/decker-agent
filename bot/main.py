"""Builds the Application, registers handlers, and runs long-polling.

Security: only TELEGRAM_USER_ID can interact with the bot (see bot/auth.py).
"""
import logging
import logging.handlers
from pathlib import Path

from telegram.ext import Application

from bot.config import BOT_TOKEN
from bot.handlers import register_handlers

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "bot.log"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _configure_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        format=_LOG_FORMAT,
        level=logging.INFO,
        handlers=[logging.StreamHandler(), file_handler],
    )
    # httpx logs one line per Telegram long-polling request (every ~10s) at
    # INFO — pure noise that buries the scraper's own progress lines in the
    # log file. Warnings/errors (actual connectivity problems) still show.
    logging.getLogger("httpx").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger(__name__)
logger.info("Logging to %s", LOG_FILE)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    register_handlers(app)

    logger.info("Bot started — long-polling active")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
