"""Where the Notion token lives.

The token is a secret and is never written to the database file, which is
meant to be copied, exported and shared. It is looked up, in order:

1. the ``NOTION_TOKEN`` environment variable;
2. the operating system keychain, when the optional ``keyring`` package is
   installed;
3. a ``secrets.json`` file in the user's configuration folder, readable only
   by the user.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ENV_TOKEN = "NOTION_TOKEN"
_KEYRING_SERVICE = "bookmark-atlas"
_KEYRING_USER = "notion-token"


def config_dir() -> Path:
    """Folder for per-user settings: ``$ATLAS_CONFIG_DIR`` or the platform default."""
    override = os.environ.get("ATLAS_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "bookmark-atlas"


def _secrets_path() -> Path:
    return config_dir() / "secrets.json"


def _keyring():
    """The keyring module if it is installed and has a working backend."""
    if os.environ.get("ATLAS_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    if isinstance(keyring.get_keyring(), FailKeyring):
        return None
    return keyring


def _read_file() -> dict:
    try:
        return json.loads(_secrets_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_file(data: dict) -> None:
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created with owner-only permissions from the start, not chmod-ed after.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def token_source() -> str | None:
    """Where the current token comes from: environment, keychain, file or None."""
    if os.environ.get(ENV_TOKEN):
        return "environment"
    keyring = _keyring()
    if keyring and keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER):
        return "keychain"
    if _read_file().get("notion_token"):
        return "file"
    return None


def get_token() -> str | None:
    if os.environ.get(ENV_TOKEN):
        return os.environ[ENV_TOKEN].strip()
    keyring = _keyring()
    if keyring:
        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if stored:
            return stored
    return _read_file().get("notion_token") or None


def set_token(token: str) -> str:
    """Store the token. Returns where it went: 'keychain' or 'file'."""
    token = token.strip()
    if not token:
        raise ValueError("The token is empty.")
    keyring = _keyring()
    if keyring:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
        return "keychain"
    data = _read_file()
    data["notion_token"] = token
    _write_file(data)
    return "file"


def delete_token() -> None:
    """Forget the stored token. An environment variable is left alone."""
    keyring = _keyring()
    if keyring and keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER):
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
    data = _read_file()
    if data.pop("notion_token", None) is not None:
        _write_file(data)


def mask(token: str | None) -> str | None:
    """A form safe to display: enough to recognise the token, not to use it."""
    if not token:
        return None
    return token[:4] + "…" + token[-4:] if len(token) > 12 else "…"
