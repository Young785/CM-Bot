#!/usr/bin/env python3
"""Run one bot cycle (same as codementor_bot_hybrid.py)."""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cmbot.bot.hybrid import main

if __name__ == "__main__":
    asyncio.run(main())
