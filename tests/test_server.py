import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from atlas import db, settings
from atlas.cli import main
from atlas.importer import import_csv
from atlas.notion import NotionClient
from atlas.server import App, make_server
from fake_notion import VALID_TOKEN, FakeNotion, make_page

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.csv"


@pytest.fixture
def fake():
    notion = FakeNotion()
    notion.add_table(
        "ds-bookmarks",
        "Veille",
        [
            make_page(
                "p1",
                "First",
                "https://example.org/1",
                tags=["A"],
                created="2026-06-01T10:00:00.000Z",
            ),
            make_page("p2", "Second", "https://example.org/2", created="2026-06-02T10:00:00.000Z"),
        ],
    )
    return notion


@pytest.fixture
def site(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    path = tmp_path / "atlas.sqlite"
    main(["--db", str(path), "init"])

    def client():
        token = settings.get_token()
        if not token:
            raise ValueError("No Notion token yet. Enter one above.")
        return NotionClient(token, transport=fake.transport, sleep=lambda s: None)

    def check(token):
        return NotionClient(token, transport=fake.transport, sleep=lambda s: None).me()

    app = App(path, client_factory=client, token_checker=check)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Site:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        token = app.session_token
        db_path = path

        def call(self, method, route, body=None, *, token="default", host=None):
            headers = {}
            if token == "default":
                token = self.token
            if token:
                headers["X-Atlas-Token"] = token
            if host:
                headers["Host"] = host
            data = json.dumps(body).encode() if body is not None else None
            if data:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                self.base + route, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    raw, code, kind = (
                        response.read(),
                        response.status,
                        response.headers["Content-Type"],
                    )
            except urllib.error.HTTPError as error:
                raw, code, kind = error.read(), error.code, error.headers["Content-Type"]
            return code, (json.loads(raw) if "json" in kind else raw.decode())

    yield Site()
    server.shutdown()
    server.server_close()


def test_page_is_served_with_its_session_token(site):
    code, html = site.call("GET", "/", token=None)
    assert code == 200
    assert "Bookmark Atlas" in html
    assert site.token in html and "__ATLAS_SESSION_TOKEN__" not in html


def test_api_refuses_calls_without_the_session_token(site):
    assert site.call("GET", "/api/state", token=None)[0] == 403
    assert site.call("GET", "/api/state", token="wrong")[0] == 403
    assert site.call("POST", "/api/notion/sync", {}, token=None)[0] == 403


def test_requests_addressed_to_another_host_are_refused(site):
    code, _ = site.call("GET", "/api/state", host="evil.example:80")
    assert code == 403
    assert site.call("GET", "/", token=None, host="evil.example")[0] == 403
    assert site.call("GET", "/api/state", host=f"localhost:{site.port}")[0] == 200


def test_unknown_routes(site):
    assert site.call("GET", "/api/nope")[0] == 404
    assert site.call("GET", "/etc/passwd", token=None)[0] == 404
    assert site.call("DELETE", "/api/state")[0] == 404


def test_fresh_state(site):
    code, state = site.call("GET", "/api/state")
    assert code == 200
    assert state["status"]["bookmarks"] == 0
    assert state["notion"] == {
        "token_set": False,
        "token_source": None,
        "token_hint": None,
        "selected": None,
    }


def test_a_bad_token_is_not_stored(site):
    code, answer = site.call("POST", "/api/notion/token", {"token": "ntn_wrong"})
    assert code == 400 and "rejected the token" in answer["error"]
    assert settings.get_token() is None

    code, answer = site.call("POST", "/api/notion/token", {"token": "  "})
    assert code == 400 and "Paste the token" in answer["error"]


def test_full_settings_flow(site):
    # 1. token
    code, answer = site.call("POST", "/api/notion/token", {"token": VALID_TOKEN})
    assert code == 200
    assert answer == {
        "connection": {"name": "Atlas", "workspace": "Test space"},
        "stored_in": "file",
    }
    state = site.call("GET", "/api/state")[1]
    assert state["notion"]["token_set"] and state["notion"]["token_source"] == "file"
    assert VALID_TOKEN not in json.dumps(state)  # only a hint is ever sent back

    # 2. tables
    code, answer = site.call("GET", "/api/notion/tables")
    assert code == 200
    table = answer["tables"][0]
    assert table["title"] == "Veille" and table["guess"]["url"] == "URL"

    # 3. select, with the mapping the page would send
    code, answer = site.call(
        "POST",
        "/api/notion/select",
        {"data_source_id": table["id"], "property_map": table["guess"]},
    )
    assert code == 200 and answer["selected"]["title"] == "Veille"

    # 4. preview writes nothing, import does
    code, answer = site.call("POST", "/api/notion/sync", {"dry_run": True})
    assert code == 200 and answer["report"]["created"] == 2 and answer["status"]["bookmarks"] == 0
    code, answer = site.call("POST", "/api/notion/sync", {})
    assert code == 200 and answer["report"]["created"] == 2 and answer["status"]["bookmarks"] == 2
    code, answer = site.call("POST", "/api/notion/sync", {})
    assert answer["report"]["created"] == 0 and not answer["report"]["full"]

    # 5. bookmarks are listed, newest first, and searchable
    code, answer = site.call("GET", "/api/bookmarks")
    assert answer["total"] == 2 and [i["title"] for i in answer["items"]] == ["Second", "First"]
    assert answer["items"][1]["tags"] == ["A"]
    assert site.call("GET", "/api/bookmarks?q=first")[1]["total"] == 1

    # 6. forget
    assert site.call("DELETE", "/api/notion/token")[1] == {"token_set": False}
    code, answer = site.call("GET", "/api/notion/tables")
    assert code == 400 and "No Notion token" in answer["error"]


def test_select_and_sync_errors_are_messages_not_crashes(site):
    site.call("POST", "/api/notion/token", {"token": VALID_TOKEN})
    code, answer = site.call("POST", "/api/notion/sync", {})
    assert code == 400 and "No Notion table selected" in answer["error"]
    code, answer = site.call("POST", "/api/notion/select", {})
    assert code == 400 and "Choose a table" in answer["error"]
    code, answer = site.call("POST", "/api/notion/select", {"data_source_id": "ds-missing"})
    assert code == 400 and "Share the database" in answer["error"]
    code, answer = site.call(
        "POST",
        "/api/notion/select",
        {"data_source_id": "ds-bookmarks", "property_map": {"title": "Name"}},
    )
    assert code == 400 and "URL" in answer["error"]


def test_search_covers_tags_and_treats_input_as_text(site):
    conn = db.open_ready(site.db_path)
    import_csv(conn, FIXTURE)
    conn.close()

    assert site.call("GET", "/api/bookmarks?q=Path%20Tracing")[1]["total"] == 1
    assert site.call("GET", "/api/bookmarks?q=lantern")[1]["total"] >= 3
    # SQL metacharacters are just characters to look for.
    code, answer = site.call("GET", "/api/bookmarks?q=%27%3B%20DROP%20TABLE%20bookmarks%3B--")
    assert code == 200 and answer["total"] == 0
    assert site.call("GET", "/api/bookmarks")[1]["total"] == 5
    assert len(site.call("GET", "/api/bookmarks?limit=2&offset=4")[1]["items"]) == 1


def test_unexpected_failure_does_not_leak_details(site, monkeypatch, capsys):
    monkeypatch.setattr(App, "state", lambda self: 1 / 0)
    code, answer = site.call("GET", "/api/state")
    assert code == 500
    assert "ZeroDivision" not in json.dumps(answer)
    assert "ZeroDivisionError" in capsys.readouterr().err


def test_csv_import_through_the_api(site):
    content = FIXTURE.read_text(encoding="utf-8")  # keeps the byte-order mark, as a browser would

    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "export.csv", "content": content, "dry_run": True}
    )
    assert code == 200 and answer["report"]["created"] == 5 and answer["status"]["bookmarks"] == 0

    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "export.csv", "content": content}
    )
    assert code == 200
    assert answer["report"]["created"] == 5 and answer["report"]["pending"] == 1
    assert answer["report"]["skipped"][0] == {"id": "8", "reason": "no URL"}
    assert answer["status"]["bookmarks"] == 5

    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "export.csv", "content": content}
    )
    assert answer["report"]["created"] == 0 and answer["report"]["unchanged"] == 6


def test_csv_import_errors_are_messages(site):
    code, answer = site.call("POST", "/api/import-csv", {})
    assert code == 400 and "Choose a file" in answer["error"]
    code, answer = site.call(
        "POST",
        "/api/import-csv",
        {"filename": "odd.csv", "content": "Name,Address\nA,https://example.org\n"},
    )
    assert code == 400 and "No URL column" in answer["error"]


def test_state_reports_the_app_version(site, monkeypatch):
    import atlas

    assert site.call("GET", "/api/state")[1]["version"] == atlas.__version__
    monkeypatch.setenv("ATLAS_APP_VERSION", "9.9.9")
    assert site.call("GET", "/api/state")[1]["version"] == "9.9.9"


def test_announce_prints_a_machine_readable_address(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    env["ATLAS_CONFIG_DIR"] = str(tmp_path / "config")
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "atlas",
            "--db",
            str(tmp_path / "new" / "atlas.sqlite"),
            "ui",
            "--port",
            "0",
            "--announce",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = process.stdout.readline()
        assert line.startswith("ATLAS_READY ")
        url = json.loads(line.split(" ", 1)[1])["url"]
        with urllib.request.urlopen(url, timeout=5) as response:
            assert "Bookmark Atlas" in response.read().decode()
        assert (tmp_path / "new" / "atlas.sqlite").exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_grooming_through_the_api(site):
    conn = db.open_ready(site.db_path)
    import_csv(conn, FIXTURE)
    conn.close()

    code, answer = site.call("GET", "/api/groom/scripts")
    assert code == 200 and answer["problems"] == []
    assert [s["name"] for s in answer["scripts"]][:2] == ["merge-duplicates", "review-source-edits"]
    assert answer["user_folder"].endswith("scripts")

    code, seen = site.call("POST", "/api/groom/preview", {"script": "derive-platform"})
    assert code == 200 and seen["changes"] == 5 and seen["groups_total"] == 4
    assert site.call("GET", "/api/groom/history")[1]["runs"] == []

    # Applying needs the preview's fingerprint, and honours unticked groups.
    assert site.call("POST", "/api/groom/apply", {"script": "derive-platform"})[0] == 400
    code, answer = site.call(
        "POST", "/api/groom/apply", {"script": "derive-platform", "fingerprint": "stale"}
    )
    assert code == 400 and "Preview again" in answer["error"]
    code, answer = site.call(
        "POST",
        "/api/groom/apply",
        {
            "script": "derive-platform",
            "fingerprint": seen["fingerprint"],
            "exclude": ["platform:X"],
        },
    )
    assert code == 200 and answer["result"]["applied"] == 4
    assert answer["status"]["without_platform"] == 1

    items = site.call("GET", "/api/bookmarks?q=lantern-core")[1]["items"]
    assert items[0]["platform"] == "GitHub"

    runs = site.call("GET", "/api/groom/history")[1]["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "applied" and runs[0]["changes"] == 4
    code, answer = site.call("POST", "/api/groom/undo", {"run_id": runs[0]["run_id"]})
    assert (
        code == 200
        and answer["result"]["reverted"] == 4
        and answer["status"]["without_platform"] == 5
    )
    assert site.call("POST", "/api/groom/undo", {"run_id": runs[0]["run_id"]})[0] == 400
    assert site.call("POST", "/api/groom/preview", {"script": "nope"})[0] == 400
    assert (site.db_path.parent / "backups").is_dir()


def test_selection_batch_export_and_bundle_import_through_the_api(site):
    conn = db.open_ready(site.db_path)
    import_csv(conn, FIXTURE)
    conn.close()

    ids = site.call("GET", "/api/bookmarks/ids?q=lantern")[1]["ids"]
    assert len(ids) == 4 and len(site.call("GET", "/api/bookmarks/ids")[1]["ids"]) == 5
    names = [t["name"] for t in site.call("POST", "/api/bookmarks/tags", {"ids": ids})[1]["tags"]]
    assert names[0] == "Rendering"

    code, answer = site.call(
        "POST", "/api/bookmarks/batch", {"ids": ids, "add_tags": ["Lantern project"]}
    )
    assert code == 200 and answer["result"]["groups"] == 4
    run_id = answer["result"]["run_id"]
    assert site.call("GET", "/api/bookmarks?q=Lantern%20project")[1]["total"] == 4
    assert site.call("POST", "/api/bookmarks/batch", {"ids": ids, "add_tags": ["a,b"]})[0] == 400
    assert site.call("POST", "/api/bookmarks/batch", {"ids": [], "add_tags": ["x"]})[0] == 400

    code, shared = site.call("POST", "/api/bookmarks/export", {"ids": ids[:2]})
    assert code == 200 and shared["count"] == 2 and shared["format"] == "bookmark-atlas-bundle"
    assert site.call("POST", "/api/bookmarks/export", {"ids": []})[0] == 400

    assert site.call("POST", "/api/groom/undo", {"run_id": run_id})[0] == 200
    assert site.call("GET", "/api/bookmarks?q=Lantern%20project")[1]["total"] == 0

    # The same import card takes a bundle: here, one new link and one already present.
    shared["bookmarks"].append(
        {"url": "https://example.org/brand-new", "title": "Brand new", "tags": ["Shared"]}
    )
    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "x.atlas.json", "content": json.dumps(shared)}
    )
    assert code == 200 and answer["report"]["created"] == 1 and answer["status"]["bookmarks"] == 6
    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "x.json", "content": '{"nope": 1}'}
    )
    assert code == 400 and "not a Bookmark Atlas bundle" in answer["error"]
    code, answer = site.call(
        "POST", "/api/import-csv", {"filename": "x.json", "content": "{broken"}
    )
    assert code == 400 and "neither" in answer["error"]


def test_state_names_the_data_folder(site):
    state = site.call("GET", "/api/state")[1]
    assert state["data_folder"] == str(site.db_path.resolve().parent)


def test_progress_follows_what_has_been_done(tmp_path):
    from atlas import db as database
    from atlas.cli import main
    from atlas.server import App as Application

    target = tmp_path / "atlas.sqlite"
    main(["--db", str(target), "init"])
    app = Application(target)
    start = app.state()["progress"]
    assert start == {
        "bookmarks": False,
        "groomed": False,
        "clustered": False,
        "mapped": False,
        "tree": False,
        "saved_view": False,
        "hidden": False,
    }
    conn = database.open_ready(target)
    conn.execute(
        "INSERT INTO bookmarks (id, url, url_normalized, domain, title, created_at, updated_at) "
        "VALUES ('b1', 'https://e.org/1', 'e.org/1', 'e.org', 'one', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO runs (id, created_at, kind, algorithm, params, app_version) "
        "VALUES ('r', '2026-01-01', 'cluster', 'x', '{}', 'x')"
    )
    conn.commit()
    now = app.state()["progress"]
    assert now["bookmarks"] and now["clustered"] and not now["mapped"]
    assert app.tutorial({"hidden": True})["progress"]["hidden"] is True
    assert app.state()["progress"]["hidden"] is True
    assert app.tutorial({})["progress"]["hidden"] is False
