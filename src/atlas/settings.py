"""Where secrets live: the Notion token, and passwords or keys of language models.

A secret is never written to the database file, which is meant to be copied,
exported and shared, nor to the data folder, which people sync and hand over.
The Notion token is looked up, in order:

1. the ``NOTION_TOKEN`` environment variable;
2. the operating system keychain, when the ``keyring`` package is installed
   and the system has a keychain that answers (the desktop app bundles it);
3. a ``secrets.json`` file readable only by the user, in ``private_dir()``:
   a folder of this computer's user, not the data folder.

Older versions wrote that file in the data folder: ``settle()`` moves what it
finds there to the keychain or the private folder and removes the old file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ENV_TOKEN = "NOTION_TOKEN"
_KEYRING_SERVICE = "bookmark-atlas"
_KEYRING_USER = "notion-token"
_FILE_TOKEN = "notion_token"


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


def private_dir() -> Path:
    """Where what must not travel with the data is kept: ``$ATLAS_SECRETS_DIR``
    (the desktop app sets it to its own folder), else the settings folder."""
    override = os.environ.get("ATLAS_SECRETS_DIR")
    return Path(override) if override else config_dir()


def _secrets_path() -> Path:
    return private_dir() / "secrets.json"


def _keyring():
    """The keyring module if it is installed and has a working backend."""
    if os.environ.get("ATLAS_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
    except Exception:  # noqa: BLE001 - not installed, or a backend that cannot even load
        return None
    return keyring


# A keychain can be installed and still refuse (locked, no session): every
# call is guarded, and a refusal means "use the file".
def _kc_get(user: str) -> str | None:
    keyring = _keyring()
    if not keyring:
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, user) or None
    except Exception:  # noqa: BLE001
        return None


def _kc_set(user: str, value: str) -> bool:
    keyring = _keyring()
    if not keyring:
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, user, value)
        return keyring.get_password(_KEYRING_SERVICE, user) == value
    except Exception:  # noqa: BLE001
        return False


def _kc_delete(user: str) -> None:
    keyring = _keyring()
    if not keyring:
        return
    try:
        if keyring.get_password(_KEYRING_SERVICE, user):
            keyring.delete_password(_KEYRING_SERVICE, user)
    except Exception:  # noqa: BLE001, S110 - nothing there to delete
        pass


def _read_at(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_at(path: Path, data: dict) -> None:
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created with owner-only permissions from the start, not chmod-ed after.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def settle() -> int:
    """Move secrets out of the places older versions used. Returns how many moved.

    A ``secrets.json`` in the data folder goes to the keychain, or to the
    private folder when there is no keychain; one in the private folder goes
    to the keychain once there is one. A secret already kept is never
    replaced: a folder received from someone else cannot change your keys.
    """
    old, new = config_dir() / "secrets.json", _secrets_path()
    places = [old] if old != new else []
    if _keyring():
        places.append(new)
    moved = 0
    for place in places:
        if not place.exists():
            continue
        left = {}
        for name, value in _read_at(place).items():
            if not isinstance(value, str) or not value:
                continue
            user = _KEYRING_USER if name == _FILE_TOKEN else name
            if _kc_get(user):
                continue  # yours stays
            if _kc_set(user, value):
                moved += 1
            elif place != new:
                mine = _read_at(new)
                if name not in mine:
                    _write_at(new, {**mine, name: value})
                    moved += 1
            else:
                left[name] = value  # the keychain refused: it stays in the private file
        try:
            _write_at(place, left)
        except OSError:
            pass  # a folder we cannot write in: nothing more to do
    return moved


def _read_file() -> dict:
    settle()
    return _read_at(_secrets_path())


def _write_file(data: dict) -> None:
    _write_at(_secrets_path(), data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A request that carries a token, a password or a key goes to the address
    it was meant for and nowhere else: a redirect is an error, not a hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def open_direct(request, timeout: float):
    """``urlopen`` that never follows a redirect. System proxies still apply."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def kept_in() -> str:
    """Where a secret saved now would go: 'keychain' or 'file'."""
    return "keychain" if _keyring() else "file"


def token_source() -> str | None:
    """Where the current token comes from: environment, keychain, file or None."""
    if os.environ.get(ENV_TOKEN):
        return "environment"
    settle()
    if _kc_get(_KEYRING_USER):
        return "keychain"
    if _read_file().get(_FILE_TOKEN):
        return "file"
    return None


def get_token() -> str | None:
    if os.environ.get(ENV_TOKEN):
        return os.environ[ENV_TOKEN].strip()
    return _get(_KEYRING_USER, _FILE_TOKEN)


def set_token(token: str) -> str:
    """Store the token. Returns where it went: 'keychain' or 'file'."""
    token = token.strip()
    if not token:
        raise ValueError("The token is empty.")
    return _set(_KEYRING_USER, _FILE_TOKEN, token)


def delete_token() -> None:
    """Forget the stored token. An environment variable is left alone."""
    _delete(_KEYRING_USER, _FILE_TOKEN)


def mask(token: str | None) -> str | None:
    """A form safe to display: enough to recognise the token, not to use it."""
    if not token:
        return None
    return token[:4] + "…" + token[-4:] if len(token) > 12 else "…"


def _get(user: str, name: str) -> str | None:
    settle()
    return _kc_get(user) or _read_at(_secrets_path()).get(name) or None


def _set(user: str, name: str, value: str) -> str:
    settle()
    data = _read_at(_secrets_path())
    if _kc_set(user, value):
        if data.pop(name, None) is not None:
            _write_file(data)  # not in two places
        return "keychain"
    data[name] = value
    _write_file(data)
    return "file"


def _delete(user: str, name: str) -> None:
    settle()
    _kc_delete(user)
    data = _read_at(_secrets_path())
    if data.pop(name, None) is not None:
        _write_file(data)


# ---- other secrets ------------------------------------------------------------
# Kept the same way as the Notion token: the system keychain when there is one,
# else the owner-only file. ``name`` is a short fixed label such as "llm-key".


def get_secret(name: str) -> str | None:
    return _get(name, name)


def set_secret(name: str, value: str) -> str:
    """Store a secret. Returns where it went: 'keychain' or 'file'."""
    value = value.strip()
    if not value:
        raise ValueError("Nothing to store.")
    return _set(name, name, value)


def delete_secret(name: str) -> None:
    _delete(name, name)
