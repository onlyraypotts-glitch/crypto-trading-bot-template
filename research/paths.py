"""Shared locations for the research tools. Nothing here is hard-coded to one machine.

BOT_DATA_DIR      runtime data (default: <repo>/data, gitignored)
SHADOW_DATA_DIR   overrides BOT_DATA_DIR for the shadow tracker / receipts / narrow model (used by selftests)
SHADOW_SCANS_DIR  scanner outputs (default: $BOT_DATA_DIR/scans)
BOT_REPORTS_DIR   human-readable reports and watchlists (default: $BOT_DATA_DIR/reports)
GROKBOT_HTTP_DIR  shared rate-limiter state + HTTP cache (default: $BOT_DATA_DIR/.http)
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
BASE_DATA_DIR = os.path.abspath(os.environ.get("BOT_DATA_DIR") or os.path.join(REPO_DIR, "data"))
DATA_DIR = os.path.abspath(os.environ.get("SHADOW_DATA_DIR") or BASE_DATA_DIR)
SCANS_DIR = os.path.abspath(os.environ.get("SHADOW_SCANS_DIR") or os.path.join(BASE_DATA_DIR, "scans"))
REPORTS_DIR = os.path.abspath(os.environ.get("BOT_REPORTS_DIR") or os.path.join(BASE_DATA_DIR, "reports"))
HTTP_DIR = os.path.abspath(os.environ.get("GROKBOT_HTTP_DIR") or os.path.join(BASE_DATA_DIR, ".http"))
