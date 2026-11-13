"""Reading the links of a MySQL/MariaDB dump. The dump below is invented."""

import sqlite3

import pytest

from atlas import db, sqldump, workspaces
from atlas.cli import main
from atlas.server import App


def fresh(path):
    conn = db.connect(path, create=True)
    db.migrate(conn)
    return conn


def broken(text: str) -> str:
    """Text as an old site wrote it out: UTF-8 bytes read as Windows-1252."""
    return "".join(
        bytes([byte]).decode("cp1252", "replace").replace("\ufffd", chr(byte))
        for byte in text.encode("utf-8")
    )


LONG = "https://example.org/" + "a" * 40
DUMP = (
    """-- phpMyAdmin SQL Dump
SET NAMES utf8mb4;

CREATE TABLE `posts` (
  `post_id` int(10) NOT NULL,
  `post_date_added` int(10) NOT NULL,
  `post_title` varchar(250) NOT NULL,
  `post_desc` varchar(90) NOT NULL,
  `post_url` varchar(60) NOT NULL,
  `post_author` varchar(250) NOT NULL,
  `post_author_username` varchar(150) NOT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

INSERT INTO `posts` (`post_id`, `post_date_added`, `post_title`, `post_desc`, `post_url`, `post_author`, `post_author_username`) VALUES
(1, 1178275027, '"""
    + broken("Scénographie d’été")
    + """', 'It\\'s a (test), with ''quotes''; and a semicolon', 'http://example.org/one', 'a.person', 'a.person@school.example'),
(2, 1200000000, 'Second', '', 'http://example.org/two', 'b.person', 'b.person@school.example'),
(3, 1300000000, 'Second, saved again with a longer title', 'Described this time', 'https://example.org/two/', 'c.person', 'c.person@school.example'),
(4, 1400000000, 'Cut link', '"""
    + "d" * 90
    + """', '"""
    + LONG
    + """', 'a.person', 'a.person@school.example'),
(5, 1400000001, 'A local file', '', 'file:///C:/Users/someone/doc.pdf', 'a.person', 'a.person@school.example'),
(6, 1400000002, '', 'Write to someone@mail.example &amp;amp; say l\\\\\\'impression est bonne', 'http://example.org/six', 'a.person', 'a.person@school.example');
INSERT INTO `posts` (`post_id`, `post_date_added`, `post_title`, `post_desc`, `post_url`, `post_author`, `post_author_username`) VALUES
(7, 1500000000, 'From a second statement', NULL, 'http://example.org/seven', 'a.person', 'a.person@school.example');

CREATE TABLE `tags` (
  `tag_id` int(10) NOT NULL,
  `tag_name` varchar(150) NOT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

INSERT INTO `tags` (`tag_id`, `tag_name`) VALUES
(1, 'design'),
(2, '"""
    + broken("numérique")
    + """'),
(3, 'only-once');

CREATE TABLE `tags_posts` (
  `post_id` int(10) DEFAULT NULL,
  `tag_id` int(10) DEFAULT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

INSERT INTO `tags_posts` (`post_id`, `tag_id`) VALUES
(1, 1),
(1, 2),
(2, 1),
(3, 2),
(7, 3),
(99, 1);

CREATE TABLE `categories` (
  `category_id` int(10) NOT NULL,
  `category_name` varchar(150) NOT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

INSERT INTO `categories` (`category_id`, `category_name`) VALUES
(1, 'Veilles');

CREATE TABLE `users` (
  `user_id` int(10) NOT NULL,
  `user_email` varchar(150) NOT NULL,
  `user_password` varchar(100) NOT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

INSERT INTO `users` (`user_id`, `user_email`, `user_password`) VALUES
(1, 'a.person@school.example', '5f4dcc3b5aa765d61d8327deb882cf99');
"""
)


def test_repair_text_only_touches_what_was_broken():
    assert sqldump.repair_text(broken("Scénographie")) == "Scénographie"
    assert sqldump.repair_text(broken(broken("été"))) == "été"  # misread twice
    assert sqldump.repair_text(broken("l’été — “ici” … œuvre")) == "l’été — “ici” … œuvre"
    for fine in ("Déjà vu, garçon", "ÉNERGIE dans le BÂTIMENT", "São Paulo", "plain", ""):
        assert sqldump.repair_text(fine) == fine
    assert sqldump.repair_text(None) == ""
    # Half a character at the end, in text broken elsewhere, went with the column's cut.
    assert sqldump.repair_text(broken("été coupé")[:-1]) == "été coup"
    assert sqldump.repair_text("un café") == "un café"


def test_tables_are_read_without_running_anything():
    tables = sqldump.read_tables(DUMP)
    assert [len(tables[name]) for name in ("posts", "tags", "tags_posts", "users")] == [7, 3, 6, 1]
    first = tables["posts"][0]
    assert first["post_id"] == 1 and first["post_date_added"] == 1178275027
    assert first["post_desc"] == "It's a (test), with 'quotes'; and a semicolon"
    assert tables["posts"][6]["post_desc"] is None
    assert sqldump.column_widths(DUMP)["posts"]["post_url"] == 60
    assert sqldump.looks_like_sql_dump(DUMP) and not sqldump.looks_like_sql_dump("Name,URL\n")


def test_reading_repairs_merges_and_flags():
    reading = sqldump.read_dump(DUMP)
    by_url = {item["url"]: item for item in reading.items}
    assert reading.table == "posts" and reading.rows == 7 and len(reading.items) == 5
    one = by_url["http://example.org/one"]
    assert one["title"] == "Scénographie d’été" and one["saved_at"] == "2007-05-04T10:37:07Z"
    assert one["tags"] == ["design", "numérique"]
    # The same link saved twice: one bookmark, earliest date, fullest text, all the tags.
    two = by_url["http://example.org/two"]
    assert reading.merged == 1 and two["ids"] == [2, 3]
    assert two["title"] == "Second, saved again with a longer title"
    assert two["summary"] == "Described this time" and two["saved_at"].startswith("2008-01-10")
    assert two["tags"] == ["design", "numérique"]
    # Text that filled its column was cut.
    cut = by_url[LONG]
    assert cut["tags"] == [sqldump.TRUNCATED_TAG] and cut["summary"].endswith("…")
    assert (reading.cut_links, reading.cut_texts) == (1, 1)
    # A tag used once is left out of the bookmark, kept in the record of what was read.
    seven = by_url["http://example.org/seven"]
    assert seven["tags"] == [] and seven["all_tags"] == ["only-once"]
    assert (reading.tags_kept, reading.tags_dropped) == (2, 1)
    six = by_url["http://example.org/six"]
    assert six["summary"] == "Write to [address removed] & say l'impression est bonne"
    assert reading.no_title == 1 and reading.repaired == 1
    assert [reason for _, reason in reading.skipped] == [
        "not a web URL: 'file:///C:/Users/someone/doc.pdf'"
    ]
    assert reading.left_out == ["categories", "users"]
    assert (reading.first_day, reading.last_day) == ("2007-05-04", "2017-07-14")


def test_a_link_carrying_an_address_is_left_out():
    dump = DUMP.replace("http://example.org/seven", "http://example.org/confirm?mail=x@y.example")
    reading = sqldump.read_dump(dump)
    assert len(reading.items) == 4
    assert reading.skipped[-1] == (7, "the link carries an e-mail address")


def test_a_file_without_links_says_so():
    with pytest.raises(ValueError, match="No table of links"):
        sqldump.read_dump("CREATE TABLE `a` (\n  `x` int\n);\nINSERT INTO `a` (`x`) VALUES\n(1);")


def test_a_dump_goes_into_a_workspace_of_its_own_and_nothing_personal_follows(tmp_path):
    home = tmp_path / "atlas.sqlite"
    fresh(home).close()
    made = sqldump.into_workspace(home, DUMP, "Old site")
    assert made["workspace"]["bookmarks"] == 5 and made["report"].created == 5
    target = workspaces.path_of(home, made["workspace"]["id"])
    conn = sqlite3.connect(target)
    rows = conn.execute(
        "SELECT title, saved_at, saved_source FROM bookmarks ORDER BY saved_at"
    ).fetchall()
    assert rows[0] == ("Scénographie d’été", "2007-05-04T10:37:07Z", "sqldump")
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 3  # two, and the flag
    conn.close()
    # Not one author, address or password anywhere in the file.
    data = target.read_bytes()
    for secret in (b"school.example", b"a.person", b"mail.example", b"5f4dcc3b", b"someone@"):
        assert secret not in data
    # The collection that was open is untouched.
    conn = sqlite3.connect(home)
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 0
    conn.close()


def test_importing_the_same_dump_again_adds_nothing(tmp_path):
    path = tmp_path / "atlas.sqlite"
    conn = fresh(path)
    reading = sqldump.read_dump(DUMP)
    assert sqldump.import_dump(conn, reading).created == 5
    again = sqldump.import_dump(conn, sqldump.read_dump(DUMP))
    assert (again.created, again.unchanged, again.pending) == (0, 5, 0)
    assert sqldump.import_dump(conn, reading, dry_run=True).dry_run
    conn.close()


def test_through_the_api_preview_writes_nothing_then_import_opens_the_workspace(tmp_path):
    home = tmp_path / "atlas.sqlite"
    fresh(home).close()
    app = App(home)
    seen = app.import_csv({"filename": "old_site_2019.sql", "content": DUMP, "dry_run": True})
    assert seen["workspace"] is None and seen["dump"]["bookmarks"] == 5
    assert seen["name"] == "old site 2019" and app.workspaces_list()["workspaces"] == []
    done = app.import_csv({"filename": "old_site_2019.sql", "content": DUMP})
    assert done["workspace"]["name"] == "old site 2019" and done["status"]["bookmarks"] == 5
    listed = app.workspaces_list()
    assert listed["active"] == done["workspace"]["id"] and listed["home"]["bookmarks"] == 0


def test_command_line(tmp_path, capsys):
    home = tmp_path / "atlas.sqlite"
    file = tmp_path / "site.sql"
    file.write_text(DUMP, encoding="utf-8")
    assert main(["--db", str(home), "init"]) == 0
    assert main(["--db", str(home), "import-sql", str(file), "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out and not (tmp_path / "Workspaces").exists()
    assert main(["--db", str(home), "import-sql", str(file), "--name", "Old site"]) == 0
    out = capsys.readouterr().out
    assert "new workspace “Old site”" in out and "tables not read: categories, users" in out
    assert len(workspaces.list_all(home)) == 1
