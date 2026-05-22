#!/usr/bin/env python3
"""Deprecated: use app.py (multi-user) or scripts/run_bot.py."""

import warnings

warnings.warn("bot_ui.py is deprecated; use app.py", DeprecationWarning, stacklevel=1)

from app import app  # noqa: F401

if __name__ == "__main__":
    app.run(debug=True, port=5000)
