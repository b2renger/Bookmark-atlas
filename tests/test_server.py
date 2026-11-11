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
