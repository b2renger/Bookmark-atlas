"""Reading bookmarks from browser profiles. The profiles are made up here, in
the layout each browser uses."""

import json
import plistlib
import sqlite3
from datetime import UTC, datetime

import pytest

from atlas import browsers, db
from atlas.cli import main
from atlas.importer import import_browser_items
from atlas.server import App

WEBKIT = 11_644_473_600


def chrome_time(day: str) -> str:
    seconds = datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()
    return str(int((seconds + WEBKIT) * 1_000_000))


def url(name, address, day="2024-05-01"):
    return {"type": "url", "name": name, "url": address, "date_added": chrome_time(day)}


def folder(name, *children):
    return {"type": "folder", "name": name, "children": list(children)}


def make_chrome(root, profile="Default", shown="Person 1"):
    (root / profile).mkdir(parents=True)
    tree = {
        "roots": {
            "bookmark_bar": folder(
                "Bookmarks bar",
                url("On the bar", "https://example.org/bar", "2021-03-04"),
                folder(
                    "Design",
                    url("Type", "https://example.org/type"),
                    folder("Colour", url("Palettes", "https://example.org/palettes")),
                ),
            ),
            "other": folder(
                "Other bookmarks",
                url("A script", "javascript:alert(1)"),
                url("Settings", "chrome://settings"),
            ),
            "synced": folder("Mobile bookmarks"),
            "sync_transaction_version": "12",
        },
        "version": 1,
    }
    (root / profile / "Bookmarks").write_text(json.dumps(tree), encoding="utf-8")
    local = root / "Local State"
    state = json.loads(local.read_text()) if local.exists() else {"profile": {"info_cache": {}}}
    state["profile"]["info_cache"][profile] = {"name": shown}
    local.write_text(json.dumps(state), encoding="utf-8")


def make_firefox(root, name="abcd1234.default-release"):
    (root / name).mkdir(parents=True)
    conn = sqlite3.connect(root / name / "places.sqlite")
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR);
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER DEFAULT NULL,
            parent INTEGER, position INTEGER, title LONGVARCHAR, keyword_id INTEGER,
            folder_type TEXT, dateAdded INTEGER, lastModified INTEGER, guid TEXT);
        INSERT INTO moz_places VALUES (1, 'https://example.org/fox', 'Page title of fox'),
            (2, 'https://example.org/tagged', 'Tagged page'), (3, 'place:sort=8', 'Most visited');
        INSERT INTO moz_bookmarks (id, type, fk, parent, title, dateAdded, guid) VALUES
            (1, 2, NULL, 0, '', 0, 'root________'),
            (2, 2, NULL, 1, 'menu', 0, 'menu________'),
            (3, 2, NULL, 1, 'toolbar', 0, 'toolbar_____'),
            (4, 2, NULL, 1, 'tags', 0, 'tags________'),
            (5, 2, NULL, 1, 'unfiled', 0, 'unfiled_____'),
            (10, 2, NULL, 2, 'Reading', 0, 'aaaaaaaaaaaa'),
            (11, 2, NULL, 10, 'Long reads', 0, 'bbbbbbbbbbbb'),
            (12, 2, NULL, 4, 'to-read', 0, 'cccccccccccc'),
            (20, 1, 1, 11, NULL, 1700000000000000, 'dddddddddddd'),
            (21, 1, 2, 3, 'Tagged', 1600000000000000, 'eeeeeeeeeeee'),
            (22, 1, 2, 12, NULL, 1600000000000001, 'ffffffffffff'),
            (23, 1, 2, 10, 'Tagged again', 1650000000000000, 'gggggggggggg'),
            (24, 1, 3, 3, 'Most visited', 1, 'hhhhhhhhhhhh'),
            (25, 3, NULL, 3, NULL, 1, 'iiiiiiiiiiii');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def test_chrome_family_profiles_are_found_and_read(home):
    make_chrome(home / ".config" / "google-chrome")
    make_chrome(home / ".config" / "google-chrome", "Profile 1", "Work")
    make_chrome(home / ".config" / "BraveSoftware" / "Brave-Browser")
    (home / ".config" / "opera").mkdir(parents=True)  # Opera keeps the file at the top
    (home / ".config" / "opera" / "Bookmarks").write_text(
        json.dumps({"roots": {"bookmark_bar": folder("bar", url("O", "https://example.org/o"))}})
    )
    listed = browsers.found(home, {}, "linux")
    assert [(e["browser"], e["profile"]) for e in listed] == [
        ("Chrome", "Person 1"),
        ("Chrome", "Work"),
        ("Brave", "Person 1"),
        ("Opera", ""),
    ]
    items = browsers.read(listed[0]["id"], home=home, env={}, platform="linux")
    assert [(i["title"], i["folders"]) for i in items] == [
        ("On the bar", []),
        ("Type", ["Design"]),
        ("Palettes", ["Design", "Colour"]),
        ("A script", []),
        ("Settings", []),
    ]
    assert datetime.fromtimestamp(int(items[0]["added"]), tz=UTC).date().isoformat() == "2021-03-04"
    assert browsers.summary(listed[0]["id"], home=home, env={}, platform="linux") == {
        "bookmarks": 3,
        "other": 2,
    }
    assert len(browsers.read(listed[3]["id"], home=home, env={}, platform="linux")) == 1


def test_where_each_platform_keeps_its_profiles(home):
    make_chrome(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    make_chrome(home / "elsewhere" / "Microsoft" / "Edge" / "User Data")
    make_firefox(home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles")
    windows = browsers.found(home, {"LOCALAPPDATA": str(home / "elsewhere")}, "win32")
    assert [e["browser"] for e in windows] == ["Edge", "Firefox"]  # the folder Windows names wins
    assert [e["browser"] for e in browsers.found(home, {}, "win32")] == ["Chrome", "Firefox"]
    make_chrome(home / "Library" / "Application Support" / "Google" / "Chrome")
    make_chrome(home / "Library" / "Application Support" / "Arc" / "User Data")
    make_firefox(home / "Library" / "Application Support" / "Firefox" / "Profiles")
    assert [e["browser"] for e in browsers.found(home, {}, "darwin")] == [
        "Chrome",
        "Arc",
        "Firefox",
    ]
    make_firefox(home / "snap" / "firefox" / "common" / ".mozilla" / "firefox")
    make_chrome(home / "xdg" / "chromium")
    linux = browsers.found(home, {"XDG_CONFIG_HOME": str(home / "xdg")}, "linux")
    assert [(e["browser"], e["profile"]) for e in linux] == [
        ("Chromium", "Person 1"),
        ("Firefox", "default-release"),
    ]
    assert browsers.found(home / "nobody", {}, "linux") == []


def test_firefox_folders_tags_and_dates(home):
    make_firefox(home / ".mozilla" / "firefox")
    (home / ".mozilla" / "firefox" / "Crash Reports").mkdir()
    listed = browsers.found(home, {}, "linux")
    assert [(e["browser"], e["profile"], e["kind"]) for e in listed] == [
        ("Firefox", "default-release", "firefox")
    ]
    items = browsers.read(listed[0]["id"], home=home, env={}, platform="linux")
    by_url = {i["url"]: i for i in items}
    # Kept in two folders and tagged: one bookmark with both folders and the tag.
    tagged = by_url["https://example.org/tagged"]
    assert tagged["title"] == "Tagged" and tagged["folders"] == ["Reading", "to-read"]
    assert tagged["added"] == "1600000000"
    fox = by_url["https://example.org/fox"]
    assert fox["title"] == "Page title of fox" and fox["folders"] == ["Reading", "Long reads"]
    assert "place:sort=8" in by_url and len(items) == 3  # not a web link: skipped at import
    # The profile is read from a copy: the original is untouched and not left locked.
    original = home / ".mozilla" / "firefox" / "abcd1234.default-release" / "places.sqlite"
    sqlite3.connect(original).execute(
        "INSERT INTO moz_places VALUES (9, 'x', 'y')"
    ).connection.close()


def test_safari_bookmarks_and_reading_list(home):
    (home / "Library" / "Safari").mkdir(parents=True)
    leaf = lambda title, address, **more: {  # noqa: E731
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "URLString": address,
        "URIDictionary": {"title": title},
        **more,
    }
    tree = {
        "WebBookmarkType": "WebBookmarkTypeList",
        "Title": "",
        "Children": [
            {"WebBookmarkType": "WebBookmarkTypeProxy", "Title": "History"},
            {
                "WebBookmarkType": "WebBookmarkTypeList",
                "Title": "BookmarksBar",
                "Children": [
                    leaf("Apple", "https://example.org/apple"),
                    {
                        "WebBookmarkType": "WebBookmarkTypeList",
                        "Title": "Recipes",
                        "Children": [leaf("Bread", "https://example.org/bread")],
                    },
                ],
            },
            {
                "WebBookmarkType": "WebBookmarkTypeList",
                "Title": "com.apple.ReadingList",
                "Children": [
                    leaf(
                        "Later",
                        "https://example.org/later",
                        ReadingList={"DateAdded": datetime(2023, 6, 1, 12, 0, 0)},
                    ),
                ],
            },
        ],
    }
    with (home / "Library" / "Safari" / "Bookmarks.plist").open("wb") as handle:
        plistlib.dump(tree, handle, fmt=plistlib.FMT_BINARY)
    listed = browsers.found(home, {}, "darwin")
    assert [(e["browser"], e["kind"]) for e in listed] == [("Safari", "safari")]
    assert browsers.found(home, {}, "linux") == []
    items = browsers.read(listed[0]["id"], home=home, env={}, platform="darwin")
    assert [(i["title"], i["folders"]) for i in items] == [
        ("Apple", []),
        ("Bread", ["Recipes"]),
        ("Later", ["Reading List"]),
    ]
    assert items[0]["added"] == "" and items[2]["added"].isdigit()


def test_only_a_listed_store_can_be_read_and_failures_are_sentences(home, monkeypatch):
    make_chrome(home / ".config" / "google-chrome")
    where = {"home": home, "env": {}, "platform": "linux"}
    with pytest.raises(ValueError, match="no longer there"):
        browsers.read("chromium:/etc/passwd", **where)
    store = home / ".config" / "google-chrome" / "Default" / "Bookmarks"
    store.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read .*Exporting them"):
        browsers.read(f"chromium:{store}", **where)

    def refuse(path):
        raise PermissionError

    monkeypatch.setattr(browsers, "read_chromium", refuse)
    with pytest.raises(ValueError, match="did not let its bookmarks be read"):
        browsers.read(f"chromium:{store}", **where)


def test_importing_twice_adds_nothing_and_non_web_links_are_skipped(home, tmp_path):
    make_chrome(home / ".config" / "google-chrome")
    entry = browsers.found(home, {}, "linux")[0]
    items = browsers.read(entry["id"], home=home, env={}, platform="linux")
    conn = db.connect(tmp_path / "atlas.sqlite", create=True)
    db.migrate(conn)
    assert import_browser_items(conn, items, dry_run=True).created == 3
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 0
    first = import_browser_items(conn, items)
    assert (first.created, len(first.skipped)) == (3, 2)
    again = import_browser_items(conn, items)
    assert (again.created, again.unchanged) == (0, 3)
    row = conn.execute(
        "SELECT saved_at, saved_source FROM bookmarks WHERE url = 'https://example.org/bar'"
    ).fetchone()
    assert tuple(row) == ("2021-03-04T00:00:00Z", "browser")
    tags = [r[0] for r in conn.execute("SELECT name FROM tags ORDER BY name")]
    assert tags == ["Colour", "Design"]
    conn.close()


def test_through_the_api_and_the_command_line(home, tmp_path, monkeypatch, capsys):
    make_chrome(home / ".config" / "google-chrome")
    make_firefox(home / ".mozilla" / "firefox")
    real = browsers.found
    monkeypatch.setattr(browsers, "found", lambda **_: real(home, {}, "linux"))
    path = tmp_path / "atlas.sqlite"
    assert main(["--db", str(path), "init"]) == 0
    app = App(path)
    listed = app.browsers()["browsers"]
    assert [(b["browser"], b["bookmarks"], b["other"]) for b in listed] == [
        ("Chrome", 3, 2),
        ("Firefox", 2, 1),
    ]
    seen = app.browsers({"id": listed[0]["id"], "dry_run": True})
    assert seen["report"]["created"] == 3 and seen["status"]["bookmarks"] == 0
    done = app.browsers({"id": listed[1]["id"]})
    assert done["report"]["created"] == 2 and done["status"]["bookmarks"] == 2
    assert done["report"]["skipped"][0]["reason"].startswith("not a web URL")
    capsys.readouterr()
    assert main(["--db", str(path), "import-browser"]) == 0
    out = capsys.readouterr().out
    assert "1. Chrome (Person 1): 3 bookmarks" in out and "2. Firefox (default-release)" in out
    assert main(["--db", str(path), "import-browser", "1"]) == 0
    assert "new 3" in capsys.readouterr().out
    assert app.state()["status"]["bookmarks"] == 5
