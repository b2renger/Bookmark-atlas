import json

import pytest

from atlas import db, groom, pagedates
from atlas.server import App
from test_clusters import path  # noqa: F401
from test_links_trends import add


def page(*head, body=""):
    return "<html><head>" + "".join(head) + "</head><body>" + body + "</body></html>"


def test_dates_are_read_from_structured_data_first():
    html = page(
        '<meta property="article:published_time" content="2024-01-02T03:04:05+01:00">',
        '<script type="application/ld+json">{"@graph": [{"@type": "WebSite"}, '
        '{"@type": "Article", "datePublished": "2023-12-31", "dateModified": "2025-01-01"}]}</script>',
    )
    assert pagedates.extract_published(html) == ("2023-12-31", "structured data (datePublished)")


def test_meta_tags_in_order_of_trust_and_time_element_last():
    both = page(
        '<meta name="date" content="2020-01-01">',
        '<meta property="article:published_time" content="2024-01-02T03:04:05+01:00">',
    )
    assert pagedates.extract_published(both) == (
        "2024-01-02T02:04:05Z",
        "meta tag article:published_time",
    )
    assert pagedates.extract_published(page('<meta name="citation_date" content="2024/5/7">')) == (
        "2024-05-07",
        "meta tag citation_date",
    )
    assert (
        pagedates.extract_published(page('<meta itemprop="datePublished" content="2022-08-09">'))[0]
        == "2022-08-09"
    )
    timed = page(
        body='<time datetime="2019-01-01">x</time><time pubdate datetime="2021-06-05">y</time>'
    )
    assert pagedates.extract_published(timed) == ("2021-06-05", "time element")


def test_nothing_is_guessed():
    assert (
        pagedates.extract_published(
            page(body="Posted on March 3, 2021 <time datetime='2021-03-03'>")
        )
        is None
    )
    assert pagedates.extract_published(page('<meta name="date" content="soon">')) is None
    assert pagedates.extract_published(page('<meta name="date" content="1800-01-01">')) is None
    assert pagedates.extract_published(page('<meta name="date" content="2999-01-01">')) is None
    assert (
        pagedates.extract_published(page('<script type="application/ld+json">{broken</script>'))
        is None
    )
    assert pagedates.extract_published("") is None and pagedates.extract_published("<<<>>") is None


def test_which_links_are_fetched():
    assert (
        pagedates.github_repository("https://github.com/apple/embedding-atlas")
        == "apple/embedding-atlas"
    )
    assert pagedates.github_repository("https://github.com/apple/embedding-atlas/issues/3") is None
    assert pagedates.github_repository("https://github.com/topics/webgl") is None
    assert pagedates.github_repository("https://gist.github.com/a/b") is None
    for url in (
        "ftp://e.org/x",
        "http://localhost/x",
        "http://192.168.1.4/x",
        "http://127.0.0.1:8000/",
        "http://nas.local/x",
        "http://[::1]/",
    ):
        assert pagedates.refused(url), url
    assert "X post" in pagedates.refused("https://x.com/someone/status/1890000000000000000")
    assert pagedates.refused("https://example.org/article") is None


def fake(pages):
    calls = []

    def get(url, accept="text/html"):
        calls.append(url)
        answer = pages.get(url, (404, ""))
        if isinstance(answer, Exception):
            raise answer
        return answer

    get.calls = calls
    return get


@pytest.fixture
def conn(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://blog.example.org/post", "post", "s", [])
    add(conn, "https://github.com/owner/repo", "repo", "s", [])
    add(conn, "https://plain.example.org/", "plain", "s", [])
    add(conn, "https://down.example.org/", "down", "s", [])
    add(conn, "https://x.com/a/status/1890000000000000000", "tweet", "s", [])
    add(conn, "https://dated.example.org/", "dated", "s", [], published="2020-02-02")
    return conn


PAGES = {
    "https://blog.example.org/post": (
        200,
        page('<meta property="article:published_time" content="2024-03-04T10:00:00Z">'),
    ),
    "https://api.github.com/repos/owner/repo": (
        200,
        json.dumps({"created_at": "2022-05-06T07:08:09Z"}),
    ),
    "https://plain.example.org/": (200, page()),
    "https://down.example.org/": TimeoutError("timed out"),
}


def test_lookup_records_what_it_finds_and_changes_no_bookmark(conn):
    assert pagedates.status(conn)["to_look_up"] == 5 and pagedates.status(conn)["without_date"] == 5
    get = fake(PAGES)
    result = pagedates.lookup(conn, get=get)
    assert (result["found"], result["none"], result["error"], result["skipped"]) == (2, 1, 1, 1)
    assert result["remaining"] == 0 and not result["github_limited"]
    # The dated bookmark and the X post were never fetched.
    assert sorted(get.calls) == sorted(PAGES)
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE published_at IS NOT NULL").fetchone()[0]
        == 1
    )
    facts = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM page_facts")}
    assert facts["https://github.com/owner/repo"]["found_in"] == "github"
    assert "TimeoutError" in facts["https://down.example.org/"]["detail"]
    assert pagedates.status(conn)["ready"] == 2
    # A second run has nothing to do and asks nothing.
    again = fake(PAGES)
    assert pagedates.lookup(conn, get=again)["looked_up"] == 0 and again.calls == []
    # Asking to retry tries the unreachable page again, and only that one.
    fixed = fake(
        {
            **PAGES,
            "https://down.example.org/": (200, page('<meta name="date" content="2021-01-01">')),
        }
    )
    assert pagedates.lookup(conn, retry_failed=True, get=fixed)["found"] == 1
    assert fixed.calls == ["https://down.example.org/"]


def test_github_asking_to_slow_down_stops_only_github(conn):
    get = fake({**PAGES, "https://api.github.com/repos/owner/repo": (403, "")})
    result = pagedates.lookup(conn, get=get)
    assert result["github_limited"] and result["found"] == 1 and result["remaining"] == 0
    # Nothing was recorded for the repository: it will be tried next time.
    assert (
        conn.execute("SELECT COUNT(*) FROM page_facts WHERE url LIKE '%github%'").fetchone()[0] == 0
    )
    assert pagedates.lookup(conn, get=fake(PAGES))["found"] == 1


def test_batches_go_on_until_nothing_is_left(conn):
    seen, remaining = 0, None
    for _ in range(10):
        result = pagedates.lookup(conn, limit=2, get=fake(PAGES))
        seen += result["looked_up"]
        remaining = result["remaining"]
        if not remaining:
            break
    assert seen == 5 and remaining == 0


def test_the_script_proposes_the_dates_and_can_be_undone(conn, path):  # noqa: F811
    script = groom.get_script("published-from-page")
    assert groom.preview(conn, script)["changes"] == 0  # nothing looked up yet
    pagedates.lookup(conn, get=fake(PAGES))
    shown = groom.preview(conn, script)
    assert shown["groups_total"] == 2
    lines = " ".join(line for group in shown["groups"] for line in group["lines"])
    assert "published 2024-03-04, read from the page (meta tag article:published_time)" in lines
    assert "the day the repository was created, from GitHub" in lines
    run = groom.apply(conn, script, {}, expected_fingerprint=shown["fingerprint"], db_path=path)
    values = dict(conn.execute("SELECT title, published_at FROM bookmarks"))
    assert values["post"] == "2024-03-04T10:00:00Z" and values["repo"] == "2022-05-06T07:08:09Z"
    sources = dict(conn.execute("SELECT title, published_source FROM bookmarks"))
    assert (sources["post"], sources["repo"]) == ("page", "github")
    assert groom.preview(conn, script)["changes"] == 0
    groom.undo(conn, run["run_id"], db_path=path)
    assert dict(conn.execute("SELECT title, published_at FROM bookmarks"))["post"] is None
    assert groom.preview(conn, script)["groups_total"] == 2


def test_a_link_changed_since_the_lookup_is_not_dated(conn):
    pagedates.lookup(conn, get=fake(PAGES))
    conn.execute("UPDATE bookmarks SET url = 'https://blog.example.org/other' WHERE title = 'post'")
    conn.commit()
    assert groom.preview(conn, groom.get_script("published-from-page"))["groups_total"] == 1


def test_dates_from_one_source_can_be_removed(conn, path):  # noqa: F811
    conn.execute("UPDATE bookmarks SET published_source = 'date_column' WHERE title = 'dated'")
    conn.commit()
    script = groom.get_script("clear-published-dates")
    assert groom.preview(conn, script, {"source": "x_id"})["changes"] == 0
    shown = groom.preview(conn, script, {"source": "date_column"})
    assert shown["groups_total"] == 1 and "2020-02-02" in shown["groups"][0]["lines"][0]
    run = groom.apply(
        conn,
        script,
        {"source": "date_column"},
        expected_fingerprint=shown["fingerprint"],
        db_path=path,
    )
    row = conn.execute(
        "SELECT published_at, published_source FROM bookmarks WHERE title = 'dated'"
    ).fetchone()
    assert tuple(row) == (None, None)
    groom.undo(conn, run["run_id"], db_path=path)
    row = conn.execute(
        "SELECT published_at, published_source FROM bookmarks WHERE title = 'dated'"
    ).fetchone()
    assert tuple(row) == ("2020-02-02", "date_column")
    with pytest.raises(groom.GroomError):
        groom.preview(conn, script, {"source": "anything"})


def test_lookup_through_the_api(conn, path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pagedates, "http_get", fake(PAGES))
    app = App(path)
    assert app.dates_status()["to_look_up"] == 5
    result = app.dates_lookup({"limit": 50})
    assert result["found"] == 2 and result["status"]["ready"] == 2
    names = [s["name"] for s in app.groom_scripts()["scripts"]]
    assert "published-from-page" in names and "clear-published-dates" in names


def test_http_get_reads_a_real_page_and_reports_failures():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/gone":
                self.send_error(404)
                return
            text = (
                "<meta name='date' content='2024-02-03'><title>caf\u00e9</title>" + "x" * 2_000_000
            )
            body = text.encode("latin-1")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=iso-8859-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # the reader stopped at its limit

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, text = pagedates.http_get(base + "/page")
        assert status == 200 and "caf\u00e9" in text and len(text) == pagedates.MAX_BYTES
        assert pagedates.extract_published(text) == ("2024-02-03", "meta tag date")
        assert pagedates.http_get(base + "/gone") == (404, "")
        # Through read_one, a local address is refused before any request.
        assert pagedates.read_one(base + "/page")["status"] == "skipped"
    finally:
        server.shutdown()
