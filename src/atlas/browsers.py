"""Read the bookmarks of the browsers installed on this computer, straight
from where each browser keeps them, without exporting a file first.

Three kinds of store are known:

- **Chromium family** (Chrome, Edge, Brave, Vivaldi, Opera, Chromium, Arc):
  a JSON file named ``Bookmarks`` in each profile folder;
- **Firefox family** (Firefox, LibreWolf, Waterfox, Zen): the table
  ``moz_bookmarks`` of ``places.sqlite`` in each profile folder;
- **Safari** (macOS): the property list ``~/Library/Safari/Bookmarks.plist``.

Everything is read only. Firefox keeps its database open while it runs, so a
copy is read and thrown away. Nothing but bookmarks is looked at: no history,
no passwords, no cookies.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .importer import BROWSER_ROOTS

WEBKIT_EPOCH = 11_644_473_600  # seconds from 1601-01-01, Chromium's zero, to 1970-01-01

# Where each browser keeps its profiles, by platform, under the folders below.
CHROMIUM = {
    "Chrome": {
        "win32": ["{local}/Google/Chrome/User Data"],
        "darwin": ["{support}/Google/Chrome"],
        "linux": ["{config}/google-chrome", "{flatpak}/com.google.Chrome/config/google-chrome"],
    },
    "Edge": {
        "win32": ["{local}/Microsoft/Edge/User Data"],
        "darwin": ["{support}/Microsoft Edge"],
        "linux": ["{config}/microsoft-edge"],
    },
    "Brave": {
        "win32": ["{local}/BraveSoftware/Brave-Browser/User Data"],
        "darwin": ["{support}/BraveSoftware/Brave-Browser"],
        "linux": [
            "{config}/BraveSoftware/Brave-Browser",
            "{flatpak}/com.brave.Browser/config/BraveSoftware/Brave-Browser",
        ],
    },
    "Vivaldi": {
        "win32": ["{local}/Vivaldi/User Data"],
        "darwin": ["{support}/Vivaldi"],
        "linux": ["{config}/vivaldi"],
    },
    "Opera": {
        "win32": ["{roaming}/Opera Software/Opera Stable"],
        "darwin": ["{support}/com.operasoftware.Opera"],
        "linux": ["{config}/opera"],
    },
    "Chromium": {
        "win32": ["{local}/Chromium/User Data"],
        "darwin": ["{support}/Chromium"],
        "linux": [
            "{config}/chromium",
            "{home}/snap/chromium/common/chromium",
            "{flatpak}/org.chromium.Chromium/config/chromium",
        ],
    },
    "Arc": {"win32": [], "darwin": ["{support}/Arc/User Data"], "linux": []},
}
FIREFOX = {
    "Firefox": {
        "win32": ["{roaming}/Mozilla/Firefox/Profiles"],
        "darwin": ["{support}/Firefox/Profiles"],
        "linux": [
            "{home}/.mozilla/firefox",
            "{home}/snap/firefox/common/.mozilla/firefox",
            "{flatpak}/org.mozilla.firefox/.mozilla/firefox",
        ],
    },
    "LibreWolf": {
        "win32": ["{roaming}/librewolf/Profiles"],
        "darwin": ["{support}/librewolf/Profiles"],
        "linux": ["{home}/.librewolf", "{flatpak}/io.gitlab.librewolf-community/.librewolf"],
    },
    "Waterfox": {
        "win32": ["{roaming}/Waterfox/Profiles"],
        "darwin": ["{support}/Waterfox/Profiles"],
        "linux": ["{home}/.waterfox"],
    },
    "Zen": {
        "win32": ["{roaming}/zen/Profiles"],
        "darwin": ["{support}/zen/Profiles"],
        "linux": ["{home}/.zen", "{flatpak}/app.zen_browser.zen/.zen"],
    },
}
# Firefox's own top folders, by their fixed identifiers: not worth a tag.
FIREFOX_ROOTS = {"root________", "menu________", "toolbar_____", "unfiled_____", "mobile______"}


def _places(home: Path, env: dict) -> dict[str, str]:
    return {
        "home": str(home),
        "local": env.get("LOCALAPPDATA") or str(home / "AppData" / "Local"),
        "roaming": env.get("APPDATA") or str(home / "AppData" / "Roaming"),
        "support": str(home / "Library" / "Application Support"),
        "config": env.get("XDG_CONFIG_HOME") or str(home / ".config"),
        "flatpak": str(home / ".var" / "app"),
    }


def _platform(platform: str) -> str:
    return "win32" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"


def found(home: Path | None = None, env: dict | None = None, platform: str | None = None):
    """Every bookmark store on this computer: ``{id, browser, profile, kind,
    path}``. The ``id`` is what ``read`` takes."""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    platform = _platform(platform or sys.platform)
    places = _places(home, env)
    out = []
    for browser, by_platform in CHROMIUM.items():
        for pattern in by_platform.get(platform, []):
            root = Path(pattern.format(**places))
            if not root.is_dir():
                continue
            names = {}
            try:
                state = json.loads((root / "Local State").read_text(encoding="utf-8"))
                for folder, info in state["profile"]["info_cache"].items():
                    names[folder] = info.get("name") or folder
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            # A profile is a folder holding a file named Bookmarks; Opera keeps one at the top.
            folders = [root] + sorted(p for p in root.iterdir() if p.is_dir())
            for folder in folders:
                store = folder / "Bookmarks"
                if store.is_file():
                    label = "" if folder == root else names.get(folder.name, folder.name)
                    out.append(_entry(browser, label, "chromium", store))
    for browser, by_platform in FIREFOX.items():
        for pattern in by_platform.get(platform, []):
            root = Path(pattern.format(**places))
            if not root.is_dir():
                continue
            for folder in sorted(p for p in root.iterdir() if p.is_dir()):
                store = folder / "places.sqlite"
                if store.is_file():
                    # "abcd1234.default-release" reads better as "default-release".
                    label = folder.name.split(".", 1)[-1]
                    out.append(_entry(browser, label, "firefox", store))
    safari = home / "Library" / "Safari" / "Bookmarks.plist"
    if platform == "darwin" and safari.exists():
        out.append(_entry("Safari", "", "safari", safari))
    return out


def _entry(browser: str, profile: str, kind: str, path: Path) -> dict:
    return {
        "id": f"{kind}:{path}",
        "browser": browser,
        "profile": profile,
        "kind": kind,
        "path": str(path),
    }


def read(source_id: str, **where) -> list[dict]:
    """The bookmarks of one store, as the importer takes them. Only a store
    that ``found`` lists can be read: a path is never taken on trust."""
    entry = next((e for e in found(**where) if e["id"] == source_id), None)
    if entry is None:
        raise ValueError("That browser profile is no longer there.")
    path = Path(entry["path"])
    try:
        if entry["kind"] == "chromium":
            return read_chromium(path)
        if entry["kind"] == "firefox":
            return read_firefox(path)
        return read_safari(path)
    except PermissionError:
        hint = (
            " On macOS, allow Bookmark Atlas under System Settings ▸ Privacy & Security ▸ "
            "Full Disk Access, or export the bookmarks from Safari (File ▸ Export) and import "
            "that file."
            if entry["kind"] == "safari"
            else ""
        )
        raise ValueError(f"{entry['browser']} did not let its bookmarks be read.{hint}") from None
    except (OSError, ValueError, KeyError, sqlite3.Error, plistlib.InvalidFileException) as error:
        raise ValueError(
            f"The bookmarks of {entry['browser']} could not be read ({type(error).__name__}). "
            "Exporting them from the browser to a file and importing that file still works."
        ) from None


def _item(url, title, folders, added) -> dict:
    kept = [f for f in folders if f and f.lower() not in BROWSER_ROOTS]
    return {
        "url": str(url or "").strip(),
        "title": " ".join(str(title or "").split()),
        "folders": list(dict.fromkeys(kept)),
        "added": str(int(added)) if added and added > 0 else "",
    }


def read_chromium(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict] = []

    def walk(node, folders):
        if not isinstance(node, dict):
            return
        if node.get("type") == "url":
            try:
                added = int(node.get("date_added") or 0) / 1_000_000 - WEBKIT_EPOCH
            except (TypeError, ValueError):
                added = 0
            items.append(_item(node.get("url"), node.get("name"), folders or [], added))
        for child in node.get("children") or []:
            walk(child, [*folders, str(node.get("name") or "")] if folders is not None else [])

    # The top folders (bookmarks bar, other, mobile) are the browser's, not the person's.
    for root in (data.get("roots") or {}).values():
        walk(root, None)
    return items


def read_firefox(path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "places.sqlite"
        shutil.copyfile(path, copy)
        for extra in ("-wal", "-shm"):  # what Firefox has not folded into the file yet
            side = path.with_name(path.name + extra)
            if side.exists():
                shutil.copyfile(side, copy.with_name(copy.name + extra))
        conn = sqlite3.connect(copy)
        try:
            conn.row_factory = sqlite3.Row
            folders = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT id, parent, title, guid FROM moz_bookmarks WHERE type = 2"
                )
            }
            tags_root = next(
                (i for i, row in folders.items() if row["guid"] == "tags________"), None
            )
            by_place: dict[int, dict] = {}
            tags: dict[int, list[str]] = {}
            rows = conn.execute(
                "SELECT b.fk, b.parent, b.title, b.dateAdded, p.url, p.title AS page_title "
                "FROM moz_bookmarks b JOIN moz_places p ON p.id = b.fk WHERE b.type = 1 "
                "ORDER BY b.dateAdded, b.id"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        parent = folders.get(row["parent"])
        if parent is not None and parent["parent"] == tags_root and tags_root is not None:
            # In Firefox a tag is a folder under a hidden root, holding the pages it marks.
            tags.setdefault(row["fk"], []).append(parent["title"])
            continue
        trail, at, seen = [], parent, set()
        while at is not None and at["id"] not in seen:
            seen.add(at["id"])
            if at["guid"] not in FIREFOX_ROOTS and at["id"] != tags_root:
                trail.append(at["title"] or "")
            at = folders.get(at["parent"])
        if any(f["id"] == tags_root for f in (folders.get(i) for i in seen) if f):
            continue
        added = (row["dateAdded"] or 0) / 1_000_000
        item = _item(row["url"], row["title"] or row["page_title"], trail[::-1], added)
        known = by_place.get(row["fk"])
        if known is None:
            by_place[row["fk"]] = item
        else:  # the same page kept in two folders: one bookmark, both folders
            known["folders"] = list(dict.fromkeys(known["folders"] + item["folders"]))
    for place, names in tags.items():
        if place in by_place:
            merged = by_place[place]["folders"] + [n for n in names if n]
            by_place[place]["folders"] = list(dict.fromkeys(merged))
    return list(by_place.values())


def read_safari(path: Path) -> list[dict]:
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    items: list[dict] = []
    own = {"BookmarksBar", "BookmarksMenu", "com.apple.ReadingList"}

    def walk(node, folders):
        if not isinstance(node, dict):
            return
        kind = node.get("WebBookmarkType")
        if kind == "WebBookmarkTypeLeaf":
            title = (node.get("URIDictionary") or {}).get("title")
            when = (node.get("ReadingList") or {}).get("DateAdded")
            added = when.timestamp() if hasattr(when, "timestamp") else 0
            items.append(_item(node.get("URLString"), title, folders, added))
        elif kind == "WebBookmarkTypeList" or "Children" in node:
            name = str(node.get("Title") or "")
            if name == "com.apple.ReadingList":
                name = "Reading List"
            elif name in own:
                name = ""
            for child in node.get("Children") or []:
                walk(child, [*folders, name] if name else folders)

    walk(data, [])
    return items


def summary(source_id: str, **where) -> dict:
    """How many bookmarks a store holds, for the list shown before importing."""
    items = read(source_id, **where)
    web = sum(1 for item in items if item["url"].lower().startswith(("http://", "https://")))
    return {"bookmarks": web, "other": len(items) - web}
