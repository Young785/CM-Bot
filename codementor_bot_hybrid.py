#!/usr/bin/env python3
"""Backward-compatible entry point — delegates to cmbot.bot.hybrid."""

from cmbot.bot.hybrid import main

if __name__ == "__main__":
    import asyncio
    
    asyncio.run(main())
