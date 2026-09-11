"""Test-wide setup.

bot/config.py reads TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID from the
environment at import time (no default — see bot/config.py), so anything
that imports bot.* (directly or transitively, e.g. bot.handlers.feed) needs
them set before that import happens. setdefault() keeps a developer's real
values if they already have them exported.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test-token")
os.environ.setdefault("TELEGRAM_USER_ID", "12345")
