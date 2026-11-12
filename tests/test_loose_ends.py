import json
import urllib.error
from socket import gaierror

from atlas import db, groom, pagedates
from atlas.groom.merge_tag_synonyms import initials, singular
from atlas.importer import ImportReport, import_record
from atlas.server import App
from test_clusters import path  # noqa: F401
from test_links_trends import add
from test_pagedates import fake, page


def preview(conn, name, params=None):
    return groom.preview(conn, groom.get_script(name), params or {})


def run(conn, path, name, params=None, exclude=None):  # noqa: F811
    shown = preview(conn, name, params)
    return groom.apply(
        conn,
        groom.get_script(name),
        params or {},
        expected_fingerprint=shown["fingerprint"],
        exclude=exclude,
        db_path=path,
    )


def tags(conn, title):
    return sorted(
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id "
            "JOIN bookmarks b ON b.id = bt.bookmark_id WHERE b.title = ?",
            (title,),
        )
    )


# ---- tag synonyms ----------------------------------------------------------


def test_word_shapes():
    assert singular("workflows") == "workflow" and singular("libraries") == "library"
    assert singular("glass") == "glass" and singular("news") == "news" and singular("bus") == "bus"
    assert initials("3D Gaussian Splatting") == "3dgs" and initials("Large Language Model") == "llm"
    assert initials("Open Source") is None


def test_plurals_and_initials_are_merged_into_the_most_used(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://e.org/1", "one", "s", ["Workflow", "LLM"])
    add(conn, "https://e.org/2", "two", "s", ["Workflow", "Large Language Model"])
    add(conn, "https://e.org/3", "three", "s", ["Workflows", "Large Language Model", "Glass"])
    shown = preview(conn, "merge-tag-synonyms")
    assert sorted(g["title"] for g in shown["groups"]) == [
        "Large Language Model  ←  LLM",
        "Workflow  ←  Workflows",
    ]
    done = run(conn, path, "merge-tag-synonyms")
    assert tags(conn, "one") == ["Large Language Model", "Workflow"]
    assert tags(conn, "three") == ["Glass", "Large Language Model", "Workflow"]
    assert preview(conn, "merge-tag-synonyms")["changes"] == 0
    groom.undo(conn, done["run_id"], db_path=path)
    assert tags(conn, "one") == ["LLM", "Workflow"]


def test_initials_that_could_mean_two_things_are_left_alone(path):  # noqa: F811
    conn = db.open_ready(path)
    add(
        conn, "https://e.org/1", "one", "s", ["3DGS", "3D Gaussian Splatting", "3D Gaussian Splats"]
    )
    assert preview(conn, "merge-tag-synonyms")["changes"] == 0


# ---- near-duplicates -------------------------------------------------------


def test_the_same_thing_under_two_links_is_proposed_for_merging(path):  # noqa: F811
    conn = db.open_ready(path)
    summary = "A fast renderer for gaussian splats that runs in the browser with WebGPU"
    add(
        conn,
        "https://a.org/splat",
        "Fast Gaussian Splat Renderer for WebGPU",
        summary,
        ["Splats"],
        saved="2026-02-01T00:00:00Z",
    )
    add(
        conn,
        "https://b.org/mirror",
        "Fast Gaussian Splat Renderer for the WebGPU",
        summary + " today",
        ["WebGPU"],
        saved="2026-01-01T00:00:00Z",
    )
    add(
        conn,
        "https://c.org/other",
        "Fast Gaussian Splat Renderer for WebGPU",
        "Notes from a conference about typography and book design",
        [],
    )
    add(conn, "https://d.org/short", "Splat Renderer", summary, [])
    shown = preview(conn, "merge-similar")
    assert shown["groups_total"] == 1 and "looks like the same thing" in " ".join(
        shown["groups"][0]["lines"]
    )
    run(conn, path, "merge-similar")
    live = sorted(r[0] for r in conn.execute("SELECT url FROM bookmarks WHERE deleted_at IS NULL"))
    assert len(live) == 3 and live[1:] == ["https://c.org/other", "https://d.org/short"]
    kept = conn.execute("SELECT id, saved_at FROM bookmarks WHERE url = ?", (live[0],)).fetchone()
    names = sorted(
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id WHERE bt.bookmark_id = ?",
            (kept["id"],),
        )
    )
    # The survivor has the tags of both and the earliest save date.
    assert names == ["Splats", "WebGPU"] and kept["saved_at"].startswith("2026-01-01")


def test_identical_titles_without_summaries_count_but_close_ones_do_not(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://a.org/1", "Notes on radiance field capture", "", [])
    add(conn, "https://a.org/2", "Notes on radiance field capture", "", [])
    add(conn, "https://a.org/3", "Notes on radiance field capture rigs", "", [])
    shown = preview(conn, "merge-similar", {"strictness": "loose"})
    assert shown["groups_total"] == 1 and shown["changes"] == 2


# ---- edits made in Notion --------------------------------------------------


def notion_row(conn, title, summary, tag_names, edited):
    report = ImportReport()
    import_record(
        conn,
        report,
        source="csv",
        raw={
            "Name": title,
            "URL": "https://e.org/n",
            "Summary": summary,
            "Tags": ", ".join(tag_names),
        },
        url="https://e.org/n",
        title=title,
        summary=summary,
        tags=tag_names,
        date_text=None,
        external_id="page-1",
        external_edited_at=edited,
    )
    conn.commit()
    return report


def test_a_row_edited_in_notion_can_be_taken_or_kept(path):  # noqa: F811
    conn = db.open_ready(path)
    notion_row(conn, "Old title", "old summary", ["A", "B"], "2026-01-01")
    assert preview(conn, "review-source-edits")["changes"] == 0
    assert notion_row(conn, "New title", "old summary", ["A", "C"], "2026-02-01").pending == 1
    assert conn.execute("SELECT title FROM bookmarks").fetchone()[0] == "Old title"

    kept = preview(conn, "review-source-edits", {"decision": "keep"})
    assert kept["changes"] == 1 and "Notion's title: “New title”" in kept["groups"][0]["lines"][0]
    assert "Notion adds C" in kept["groups"][0]["lines"][0]

    taken = preview(conn, "review-source-edits")
    lines = taken["groups"][0]["lines"]
    assert any("title: “Old title” → “New title”" in line for line in lines)
    assert "add tag “C”" in lines and not any("remove tag" in line for line in lines)
    done = run(conn, path, "review-source-edits")
    assert conn.execute("SELECT title FROM bookmarks").fetchone()[0] == "New title"
    assert tags(conn, "New title") == ["A", "B", "C"]
    assert conn.execute("SELECT COUNT(*) FROM source_records WHERE pending").fetchone()[0] == 0
    assert preview(conn, "review-source-edits")["changes"] == 0

    groom.undo(conn, done["run_id"], db_path=path)
    assert conn.execute("SELECT title FROM bookmarks").fetchone()[0] == "Old title"
    run(conn, path, "review-source-edits", {"decision": "take", "remove_tags": True})
    assert tags(conn, "New title") == ["A", "C"]


def test_keeping_my_version_only_marks_the_row_as_seen(path):  # noqa: F811
    conn = db.open_ready(path)
    notion_row(conn, "Mine", "s", ["A"], "2026-01-01")
    notion_row(conn, "Theirs", "s", ["A"], "2026-02-01")
    run(conn, path, "review-source-edits", {"decision": "keep"})
    assert conn.execute("SELECT title FROM bookmarks").fetchone()[0] == "Mine"
    assert conn.execute("SELECT COUNT(*) FROM source_records WHERE pending").fetchone()[0] == 0


# ---- dead links ------------------------------------------------------------


def test_only_pages_that_say_they_are_gone_are_called_gone():
    answers = {
        "https://e.org/ok": (200, ""),
        "https://e.org/moved": (301, ""),
        "https://e.org/missing": (404, ""),
        "https://e.org/removed": (410, ""),
        "https://e.org/forbidden": (403, ""),
        "https://e.org/broken": (503, ""),
        "https://e.org/slow": TimeoutError("timed out"),
        "https://nowhere.example/": urllib.error.URLError(
            gaierror(-2, "Name or service not known")
        ),
        "https://refuses.example/": urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    }
    results = {url: pagedates.check_one(url, fake(answers))["result"] for url in answers}
    assert results == {
        "https://e.org/ok": "ok",
        "https://e.org/moved": "ok",
        "https://e.org/missing": "gone",
        "https://e.org/removed": "gone",
        "https://e.org/forbidden": "unsure",
        "https://e.org/broken": "unsure",
        "https://e.org/slow": "unsure",
        "https://nowhere.example/": "gone",
        "https://refuses.example/": "unsure",
    }
    assert (
        pagedates.check_one("https://x.com/a/status/1890000000000000000", fake({}))["result"]
        == "skipped"
    )


def test_dead_links_get_a_tag_and_nothing_is_deleted(path, monkeypatch):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://e.org/ok", "alive", "s", [])
    add(conn, "https://e.org/missing", "dead", "s", ["X"])
    add(conn, "https://e.org/forbidden", "shy", "s", [])
    web = fake(
        {
            "https://e.org/ok": (200, ""),
            "https://e.org/missing": (404, ""),
            "https://e.org/forbidden": (403, ""),
        }
    )
    assert pagedates.link_status(conn)["to_check"] == 3
    result = pagedates.check_links(conn, get=web)
    assert (result["ok"], result["gone"], result["unsure"], result["remaining"]) == (1, 1, 1, 0)
    assert pagedates.check_links(conn, get=fake({}))["checked"] == 0  # nothing asked twice
    shown = preview(conn, "flag-dead-links")
    assert shown["groups_total"] == 1 and "page not found" in shown["groups"][0]["lines"][0]
    run(conn, path, "flag-dead-links")
    assert tags(conn, "dead") == ["X", "dead link"] and tags(conn, "shy") == []
    assert (
        conn.execute("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL").fetchone()[0] == 3
    )
    assert preview(conn, "flag-dead-links")["changes"] == 0
    # Checking again asks only the doubtful and the dead; a page that came back is no longer proposed.
    back = fake({"https://e.org/missing": (200, ""), "https://e.org/forbidden": (200, "")})
    again = pagedates.check_links(conn, again=True, get=back)
    assert (
        again["checked"] == 2
        and again["status"]["gone"] == 0
        and sorted(back.calls) == sorted(back.calls)
    )
    assert preview(conn, "flag-dead-links", {"tag": "broken"})["changes"] == 0
    # A link that changed since the check counts as not checked.
    conn.execute("UPDATE bookmarks SET url = 'https://e.org/new' WHERE title = 'alive'")
    conn.commit()
    assert pagedates.link_status(conn)["to_check"] == 1
    monkeypatch.setattr(pagedates, "http_get", fake({"https://e.org/new": (200, "")}))
    app = App(path)
    assert app.links_check({"limit": 5})["remaining"] == 0 and app.links_check_status()["ok"] == 3


# ---- Hugging Face and YouTube dates -----------------------------------------


def test_hugging_face_links_are_recognised():
    repo = pagedates.huggingface_repository
    assert (
        repo("https://huggingface.co/black-forest-labs/FLUX.1-dev")
        == "models/black-forest-labs/FLUX.1-dev"
    )
    assert (
        repo("https://huggingface.co/datasets/codepawl/abo-glb/tree/main")
        == "datasets/codepawl/abo-glb"
    )
    assert repo("https://huggingface.co/spaces/owner/demo") == "spaces/owner/demo"
    assert repo("https://huggingface.co/gpt2") == "models/gpt2"
    for other in (
        "https://huggingface.co/docs/hub",
        "https://huggingface.co/blog/x",
        "https://huggingface.co/",
        "https://huggingface.co/datasets/owner",
        "https://example.org/a/b",
    ):
        assert repo(other) is None, other


def test_dates_from_hugging_face_and_youtube():
    web = fake(
        {
            "https://huggingface.co/api/models/owner/model": (
                200,
                json.dumps({"createdAt": "2025-03-04T05:06:07.000Z"}),
            ),
            "https://huggingface.co/api/datasets/owner/data": (404, ""),
            "https://huggingface.co/datasets/owner/data": (
                200,
                page('<meta name="date" content="2024-01-02">'),
            ),
            "https://www.youtube.com/watch?v=abc": (
                200,
                '<script>var x = {"publishDate":"2023-07-08T09:00:00-07:00","other":1}</script>',
            ),
            "https://www.youtube.com/watch?v=none": (200, "<html>consent</html>"),
            "https://example.org/x": (200, '<script>{"publishDate":"2023-07-08"}</script>'),
        }
    )
    found = pagedates.read_one("https://huggingface.co/owner/model", web)
    assert (found["published_at"], found["found_in"]) == ("2025-03-04T05:06:07Z", "hugging face")
    # When the API has nothing, the page is read like any other.
    assert (
        pagedates.read_one("https://huggingface.co/datasets/owner/data", web)["published_at"]
        == "2024-01-02"
    )
    video = pagedates.read_one("https://www.youtube.com/watch?v=abc", web)
    assert (video["published_at"], video["found_in"]) == ("2023-07-08T16:00:00Z", "video details")
    assert pagedates.read_one("https://www.youtube.com/watch?v=none", web)["status"] == "none"
    # The video rule is not applied to other sites.
    assert pagedates.read_one("https://example.org/x", web)["status"] == "none"


def test_a_hugging_face_date_is_proposed_with_its_source(path):  # noqa: F811
    conn = db.open_ready(path)
    add(conn, "https://huggingface.co/owner/model", "model", "s", [])
    pagedates.lookup(
        conn,
        get=fake(
            {
                "https://huggingface.co/api/models/owner/model": (
                    200,
                    json.dumps({"createdAt": "2025-03-04T05:06:07.000Z"}),
                )
            }
        ),
    )
    shown = preview(conn, "published-from-page")
    assert "the day it was created, from Hugging Face" in " ".join(shown["groups"][0]["lines"])
    run(conn, path, "published-from-page")
    assert conn.execute("SELECT published_source FROM bookmarks").fetchone()[0] == "huggingface"
    assert preview(conn, "clear-published-dates", {"source": "huggingface"})["groups_total"] == 1
