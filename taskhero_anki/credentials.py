from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .config import _atomic_json_write

ANKI_INTEGRATION_TOKEN_PATTERN = re.compile(r"^th-int-v1-anki-[A-Za-z0-9_-]{43}$")


def normalize_integration_token(value: str) -> str:
    token = value.strip()
    if not ANKI_INTEGRATION_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Enter a valid TaskHero Anki integration token.")
    return token


class CredentialStore:
    """Stores the integration token outside Anki collection and card data.

    The credential lives in the active profile directory with user-only modes on
    platforms that implement POSIX permissions. Windows still protects the file
    with the user's profile ACL.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Optional[str]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        api_key = value.get("api_key")
        if not isinstance(api_key, str):
            return None
        try:
            return normalize_integration_token(api_key)
        except ValueError:
            return None

    def save(self, api_key: str) -> None:
        _atomic_json_write(self.path, {"api_key": normalize_integration_token(api_key)}, 0o600)

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
