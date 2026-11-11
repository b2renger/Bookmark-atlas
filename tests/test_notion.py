import sys
from pathlib import Path

import pytest

from atlas import db, settings
from atlas.cli import main
from atlas.importer import import_csv
from atlas.notion import NOTION_VERSION, NotionClient, NotionError
from atlas.notion_sync import extract, guess_property_map, select_data_source, selected, sync
from fake_notion import SCHEMA, VALID_TOKEN, FakeNotion, make_page

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])
    connection = db.open_ready(path)
    yield connection
    connection.close()


@pytest.fixture
def fake():
    notion = FakeNotion()
    notion.add_table(
        "ds-bookmarks",
        "Veille",
        [
            make_page(
                "p1",
                "Paper Lantern Renderer",
                "https://www.example.org/lantern/",
                summary="A path tracer.",
                tags=["Rendering", "Open Source"],
                date="2026-03-03T16:12:00.000+01:00",
                created="2026-03-03T15:12:00.000Z",
            ),
            make_page(
                "p2",
                "Lantern thread",
                "https://x.com/example_dev/status/2030000000000000001",
                tags=["Rendering"],
                date="2026-06-14",
                created="2026-06-16T08:00:00.000Z",
            ),
            make_page(
                "p3",
                "lantern-core",
                "https://github.com/Example-Org/Lantern-Core",
                created="2026-07-01T09:00:00.000Z",
            ),
        ],
    )
    notion.add_table("ds-other", "Recipes", [], database_id="db-2")
    return notion


def client_for(fake, token=VALID_TOKEN):
    sleeps = []
    client = NotionClient(token, transport=fake.transport, sleep=sleeps.append, clock=lambda: 0.0)
    client.sleeps = sleeps
    return client


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# --- client -----------------------------------------------------------------


def test_requests_carry_token_and_version(fake):
    seen = {}

    def spy(method, url, headers, body):
        seen.update(headers)
        return fake.transport(method, url, headers, body)

    NotionClient(VALID_TOKEN, transport=spy, sleep=lambda s: None).me()
    assert seen["Authorization"] == f"Bearer {VALID_TOKEN}"
    assert seen["Notion-Version"] == NOTION_VERSION


def test_me_and_bad_token(fake):
    assert client_for(fake).me() == {"name": "Atlas", "workspace": "Test space"}
    with pytest.raises(NotionError, match="rejected the token"):
        client_for(fake, token="ntn_wrong").me()


def test_list_tables_follows_pagination(fake):
    fake.page_size = 1
    tables = client_for(fake).list_data_sources()

    assert [t["title"] for t in tables] == ["Recipes", "Veille"]
    veille = tables[1]
    assert veille["id"] == "ds-bookmarks" and veille["database_id"] == "db-1"
    assert {"name": "URL", "type": "url"} in veille["columns"]
    assert sum(1 for call in fake.calls if call[1] == "/search") == 2


def test_requests_are_spaced_out(fake):
    client = client_for(fake)
    client.me()
    client.me()
    assert client.sleeps == [pytest.approx(0.35)]  # the clock is frozen, so a full wait


def test_rate_limit_waits_for_retry_after_then_succeeds(fake):
    fake.fail_next = [(429, {"Retry-After": "7"})]
    client = client_for(fake)
    assert client.me()["name"] == "Atlas"
    assert 7.0 in client.sleeps
    assert client.requests_made == 2


def test_rate_limit_gives_up_eventually(fake):
    fake.fail_next = [(429, {"Retry-After": "1"})] * 10
    with pytest.raises(NotionError, match="rate limiting"):
        client_for(fake).me()


def test_unshared_table_is_explained(fake):
    with pytest.raises(NotionError, match="Share the database"):
        client_for(fake).retrieve_data_source("ds-missing")


def test_incomplete_listing_is_an_error_not_a_short_table(fake):
    fake.incomplete = True
    with pytest.raises(NotionError, match="only part of the table"):
        list(client_for(fake).query_pages("ds-bookmarks"))


# --- mapping ----------------------------------------------------------------


def test_guess_property_map(fake):
    columns = client_for(fake).retrieve_data_source("ds-bookmarks")["columns"]
    assert guess_property_map(columns) == {
        "title": "Name",
        "url": "URL",
        "summary": "Description",
        "tags": "Keywords",
        "date": "Date",
    }


def test_guess_prefers_names_over_first_column_of_a_type():
    columns = [
        {"name": "Aside", "type": "rich_text"},
        {"name": "Summary", "type": "rich_text"},
        {"name": "Title", "type": "title"},
        {"name": "Link", "type": "url"},
    ]
    assert guess_property_map(columns) == {"title": "Title", "url": "Link", "summary": "Summary"}


def test_extract_reads_each_property_type():
    page = make_page(
        "p",
        "A title",
        "https://example.org/a",
        summary="Some text",
        tags=["One", "Two"],
        date="2026-06-14",
    )
    mapping = {
        "title": "Name",
        "url": "URL",
        "summary": "Description",
        "tags": "Keywords",
        "date": "Date",
    }
    assert extract(page, mapping) == {
        "title": "A title",
        "url": "https://example.org/a",
        "summary": "Some text",
        "tags": ["One", "Two"],
        "date_text": "2026-06-14",
    }


def test_extract_copes_with_empty_values_and_unmapped_fields():
    page = make_page("p", "", None)
    assert extract(page, {"url": "URL", "title": "Name"}) == {
        "title": "",
        "url": None,
        "summary": None,
        "tags": [],
        "date_text": None,
    }


def test_extract_applies_a_separate_time_zone():
    page = make_page("p", "T", "https://example.org")
    page["properties"]["Date"]["date"] = {
        "start": "2026-07-04T18:00:00",
        "end": None,
        "time_zone": "Europe/Paris",
    }
    assert extract(page, {"url": "URL", "date": "Date"})["date_text"] == "2026-07-04T18:00:00+02:00"


# --- selecting and syncing --------------------------------------------------


def test_select_stores_choice_and_guessed_mapping(conn, fake):
    choice = select_data_source(conn, client_for(fake), "ds-bookmarks")
    assert choice["title"] == "Veille"
    assert choice["property_map"]["url"] == "URL"
    assert choice["last_synced_time"] is None
    assert selected(conn) == choice


def test_select_rejects_a_mapping_without_url(conn, fake):
    with pytest.raises(ValueError, match="URL"):
        select_data_source(conn, client_for(fake), "ds-bookmarks", {"title": "Name"})
    with pytest.raises(ValueError, match="no column named"):
        select_data_source(conn, client_for(fake), "ds-bookmarks", {"url": "Nope"})
    assert selected(conn) is None


def test_sync_without_selection_is_explained(conn, fake):
    with pytest.raises(ValueError, match="No Notion table selected"):
        sync(conn, client_for(fake))


def test_first_sync_imports_everything(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    report = sync(conn, client)

    assert report.full and report.rows == 3 and report.created == 3
    assert report.requests == 2  # three rows, two per page
    row = conn.execute("SELECT * FROM bookmarks WHERE title = 'Paper Lantern Renderer'").fetchone()
    assert row["saved_at"] == "2026-03-03T15:12:00Z"
    assert row["saved_source"] == "notion_created"
    record = conn.execute("SELECT * FROM source_records WHERE external_id = 'p1'").fetchone()
    assert record["source"] == "notion" and record["pending"] == 0
    assert selected(conn)["last_synced_time"] == "2026-07-01T09:00:00Z"


def test_sync_reports_whether_creation_time_matches_the_date_column(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    report = sync(conn, client)
    # p1 has a timed date equal to its creation time; p2 is date-only; p3 has none.
    assert (report.created_time_compared, report.created_time_agrees) == (1, 1)
    assert report.date_only == 1


def test_second_sync_asks_only_for_recent_edits_and_writes_nothing(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    sync(conn, client)
    fake.calls.clear()

    report = sync(conn, client)

    query = next(call for call in fake.calls if call[1].endswith("/query"))
    assert query[2]["filter"] == {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": "2026-07-01T09:00:00Z"},
    }
    assert not report.full
    assert report.rows == 1 and report.unchanged == 1 and report.created == 0


def test_new_row_is_picked_up(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    sync(conn, client)
    fake.tables["ds-bookmarks"]["pages"].append(
        make_page("p4", "New", "https://example.net/new", created="2026-08-01T12:00:00.000Z")
    )

    report = sync(conn, client)

    assert report.created == 1
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 4
    assert selected(conn)["last_synced_time"] == "2026-08-01T12:00:00Z"


def test_upstream_edit_waits_for_review_and_does_not_touch_the_bookmark(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    sync(conn, client)
    page = fake.tables["ds-bookmarks"]["pages"][2]
    page["properties"]["Name"]["title"][0]["plain_text"] = "Renamed upstream"
    page["last_edited_time"] = "2026-09-01T00:00:00.000Z"

    report = sync(conn, client)

    assert report.pending == 1
    assert count(conn, "SELECT COUNT(*) FROM bookmarks WHERE title = 'lantern-core'") == 1
    assert count(conn, "SELECT COUNT(*) FROM source_records WHERE external_id = 'p3'") == 1
    assert count(conn, "SELECT pending FROM source_records WHERE external_id = 'p3'") == 1


def test_deleted_bookmark_stays_deleted_when_edited_upstream(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    sync(conn, client)
    conn.execute(
        "UPDATE bookmarks SET deleted_at = ? WHERE title = 'lantern-core'", (db.now_iso(),)
    )
    conn.commit()
    page = fake.tables["ds-bookmarks"]["pages"][2]
    page["properties"]["Description"]["rich_text"] = [{"plain_text": "Edited"}]
    page["last_edited_time"] = "2026-09-01T00:00:00.000Z"

    report = sync(conn, client, full=True)

    assert report.tombstoned == 1 and report.created == 0 and report.pending == 0
    assert count(conn, "SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL") == 2


def test_notion_rows_link_quietly_to_bookmarks_first_imported_from_csv(conn, fake):
    import_csv(conn, FIXTURE)
    fake.tables["ds-bookmarks"]["pages"] = [
        # Same content as the CSV row, as the API would give it.
        make_page(
            "p1",
            "Paper Lantern Renderer",
            "https://www.example.org/lantern/?utm_source=newsletter",
            summary="An invented path tracer for paper lanterns, used here as test data.",
            tags=["Open Source", "Rendering", "Path Tracing"],  # another order
            date="2026-03-03T16:12:00.000+01:00",
            created="2026-03-03T15:12:00.000Z",
        ),
        # Same link as a CSV row with a date-only Date, same content.
        make_page(
            "p3",
            "lantern-core",
            "https://github.com/Example-Org/Lantern-Core",
            summary="Fictional repository holding the renderer core.",
            tags=["Open Source", "C++"],
            date="2026-01-09",
            created="2026-01-12T08:30:00.000Z",
        ),
    ]
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    before = count(conn, "SELECT COUNT(*) FROM bookmarks")

    report = sync(conn, client)

    assert report.linked == 2 and report.pending == 0 and report.created == 0
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == before
    # A save date that came from the CSV is kept; an empty one is filled and journaled.
    lantern = conn.execute(
        "SELECT * FROM bookmarks WHERE title = 'Paper Lantern Renderer'"
    ).fetchone()
    assert lantern["saved_source"] == "date_column"
    core = conn.execute("SELECT * FROM bookmarks WHERE title = 'lantern-core'").fetchone()
    assert (core["saved_at"], core["saved_source"]) == ("2026-01-12T08:30:00Z", "notion_created")
    assert report.filled_saved_at == 1
    assert count(conn, "SELECT COUNT(*) FROM changes WHERE entity_id = ?", core["id"]) == 1


def test_trashed_and_linkless_rows_are_not_imported(conn, fake):
    fake.tables["ds-bookmarks"]["pages"] = [
        make_page("t1", "Trashed", "https://example.org/t", in_trash=True),
        make_page("n1", "No link", None),
        make_page("b1", "Bad link", "mailto:a@example.org"),
    ]
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    report = sync(conn, client)

    assert report.in_trash == 1 and report.created == 0
    assert [key for key, _ in report.skipped] == ["n1", "b1"]
    assert report.as_dict()["skipped"][0] == {"id": "n1", "reason": "no URL"}


def test_dry_run_writes_nothing_and_keeps_the_mark(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    report = sync(conn, client, dry_run=True)

    assert report.created == 3
    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 0
    assert selected(conn)["last_synced_time"] is None


def test_failure_midway_leaves_nothing_and_keeps_the_mark(conn, fake):
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    fake.incomplete = True  # the last page reports a truncated result

    with pytest.raises(NotionError):
        sync(conn, client)

    assert count(conn, "SELECT COUNT(*) FROM bookmarks") == 0
    assert selected(conn)["last_synced_time"] is None


# --- token storage ----------------------------------------------------------


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    return tmp_path / "config"


def test_token_round_trip_in_a_private_file(config):
    assert settings.get_token() is None and settings.token_source() is None

    assert settings.set_token("  ntn_abcdefghijklmnop  ") == "file"

    assert settings.get_token() == "ntn_abcdefghijklmnop"
    assert settings.token_source() == "file"
    secrets = config / "secrets.json"
    if sys.platform != "win32":  # Windows has no owner-only mode bits to check
        assert secrets.stat().st_mode & 0o777 == 0o600

    settings.delete_token()
    assert settings.get_token() is None


def test_environment_variable_wins(config, monkeypatch):
    settings.set_token("ntn_from_file_000000")
    monkeypatch.setenv("NOTION_TOKEN", "ntn_from_env_0000000")
    assert settings.get_token() == "ntn_from_env_0000000"
    assert settings.token_source() == "environment"


def test_empty_token_is_refused(config):
    with pytest.raises(ValueError):
        settings.set_token("   ")


def test_mask_never_shows_the_whole_token():
    assert settings.mask("ntn_abcdefghijklmnop") == "ntn_…mnop"
    assert settings.mask("short") == "…"
    assert settings.mask(None) is None


def test_token_is_not_in_the_database(conn, fake, config, tmp_path):
    settings.set_token(VALID_TOKEN)
    client = client_for(fake)
    select_data_source(conn, client, "ds-bookmarks")
    sync(conn, client)
    conn.close()
    for path in tmp_path.glob("*.sqlite*"):
        assert VALID_TOKEN.encode() not in path.read_bytes()


def test_schema_used_by_the_fake_matches_what_the_guess_expects():
    assert {"title", "url", "rich_text", "multi_select", "date"} <= {
        column["type"] for column in SCHEMA.values()
    }


# --- command line -----------------------------------------------------------


def test_cli_tables_select_sync(tmp_path, fake, config, monkeypatch, capsys):
    import atlas.cli as cli

    path = tmp_path / "cli.sqlite"
    main(["--db", str(path), "init"])
    monkeypatch.setattr(cli, "_notion_client", lambda: client_for(fake))

    assert main(["--db", str(path), "notion", "tables"]) == 0
    assert "ds-bookmarks  Veille  (6 columns)" in capsys.readouterr().out

    assert main(["--db", str(path), "notion", "select", "ds-bookmarks"]) == 0
    assert "url      <- URL" in capsys.readouterr().out

    assert main(["--db", str(path), "sync", "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out

    assert main(["--db", str(path), "sync"]) == 0
    out = capsys.readouterr().out
    assert "Synced Veille (full, 2 requests)" in out
    assert "matches the Date column on 1 of 1" in out

    assert main(["--db", str(path), "sync"]) == 0
    assert "incremental" in capsys.readouterr().out


def test_cli_without_token_says_what_to_do(tmp_path, config, capsys):
    path = tmp_path / "cli.sqlite"
    main(["--db", str(path), "init"])
    assert main(["--db", str(path), "notion", "tables"]) == 1
    assert "atlas notion token" in capsys.readouterr().err


def test_cli_reports_notion_errors_without_a_traceback(tmp_path, fake, config, monkeypatch, capsys):
    import atlas.cli as cli

    path = tmp_path / "cli.sqlite"
    main(["--db", str(path), "init"])
    monkeypatch.setattr(cli, "_notion_client", lambda: client_for(fake, token="ntn_wrong"))
    assert main(["--db", str(path), "notion", "tables"]) == 1
    assert "rejected the token" in capsys.readouterr().err
