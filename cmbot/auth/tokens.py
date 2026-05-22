"""Load and persist Codementor session tokens (A1 per-user, A2 global)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from cmbot.paths import GLOBAL_TOKENS_FILE, IS_ADMIN, USER_ID, USER_TOKENS_FILE
from cmbot.storage.json_store import load_json, load_json_dict, save_json

logger = logging.getLogger(__name__)


def load_tokens() -> Dict[str, Dict]:
    tokens: Dict[str, Dict] = {}
    global_tokens = load_json_dict(GLOBAL_TOKENS_FILE)
    if global_tokens.get("A2"):
        tokens["A2"] = global_tokens["A2"]
        logger.info("Loaded A2 token from global file")

    user_tokens = load_json_dict(USER_TOKENS_FILE)
    if user_tokens.get("A1"):
        tokens["A1"] = user_tokens["A1"]
        logger.info("Loaded A1 token from user file")
    return tokens


def save_tokens(tokens: Dict[str, Dict]) -> None:
    if USER_TOKENS_FILE and "A1" in tokens:
        user_tokens = {"A1": tokens["A1"]}
        try:
            USER_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            save_json(USER_TOKENS_FILE, user_tokens)
            logger.info("Saved A1 token to user file")
        except OSError as e:
            logger.error("Failed to save user tokens: %s", e)

    if IS_ADMIN or not USER_ID:
        try:
            global_tokens = load_json_dict(GLOBAL_TOKENS_FILE)
            if "A2" in tokens:
                global_tokens["A2"] = tokens["A2"]
                GLOBAL_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
                save_json(GLOBAL_TOKENS_FILE, global_tokens)
                logger.info("Saved A2 token to global file")
        except OSError as e:
            logger.error("Failed to save global tokens: %s", e)


def load_merged_tokens(user_tokens_path: Path) -> Dict:
    """A1 from user file; A2 from global session_tokens.json."""
    merged = dict(load_json(user_tokens_path, {}))
    global_data = load_json(GLOBAL_TOKENS_FILE, {})
    a2 = global_data.get("A2") if isinstance(global_data, dict) else None
    if isinstance(a2, dict) and a2.get("access_token"):
        merged["A2"] = a2
    return merged
