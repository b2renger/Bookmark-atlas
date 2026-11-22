"""Notebooks: answers that rest on the words of the sources."""

import json

import pytest

from atlas import assist, db, llm, notebook
from atlas.server import App
from fake_farm import FakeFarm
from test_assist import fill, pages
from test_clusters import path  # noqa: F401

RIVERS = """<html><head><title>Rivers</title></head><body><article>
<p>Cartographers simplify the hydrography network differently at every scale of the map.</p>
<p>The Danube crosses ten countries before it reaches the Black Sea through a wide delta.</p>
<p>Open elevation datasets for Europe are published under permissive licences since 2019.</p>
</article></body></html>"""
BREAD = """<html><head><title>Bread</title></head><body><article>
<p>A sourdough starter needs flour and water and about a week of daily feeding at home.</p>
<p>Steam in the oven during the first ten minutes gives the loaf its thin crisp crust.</p>
<p>Whole grain flours ferment faster and want more water than white flour does.</p>
</article></body></html>"""


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")


@pytest.fixture
def farm():
    made = FakeFarm()
    yield made
    made.close()


@pytest.fixture
def conn(path):  # noqa: F811
    connection = db.open_ready(path)
    yield connection
    connection.close()


@pytest.fixture
def read(conn):
    ids = fill(conn)
    urls = {"https://example.org/map0": RIVERS, "https://example.org/cook0": BREAD}
    assist.read_pages(conn, limit=40, get=pages(urls))
    return ids


def test_a_page_is_cut_into_passages_at_paragraphs_then_sentences():
    assert notebook.cut("") == []
    short = notebook.cut("One short paragraph.\n\nAnother short one.")
    assert short == ["One short paragraph.\nAnother short one."]  # small ones travel together
    long = "This sentence is exactly forty characters. " * 60
    pieces = notebook.cut(long)
    assert len(pieces) > 2 and all(len(p) <= notebook.PASSAGE_MOST for p in pieces)
    assert all(p.endswith(".") for p in pieces)
    assert "".join(pieces).replace(" ", "") == long.replace(" ", "")  # nothing lost
    assert len(notebook.cut("word " * 100_000)) == notebook.PER_BOOKMARK


def test_every_source_has_a_card_and_read_pages_have_more(conn, read):
    done = notebook.prepare(conn, [read["map0"], read["map1"], "gone"])
    assert done == {"sources": 2, "with_page": 1, "passages": 3}
    rows = conn.execute(
        "SELECT ord, text FROM passages WHERE bookmark_id = ? ORDER BY ord", (read["map0"],)
    ).fetchall()
    assert rows[0]["text"].startswith("River maps 0\n") and "Tags: data, maps" in rows[0]["text"]
    assert "Danube" in rows[1]["text"]
    before = [r[0] for r in conn.execute("SELECT id FROM passages ORDER BY id")]
    notebook.prepare(conn, [read["map0"], read["map1"]])
    assert before == [
        r[0] for r in conn.execute("SELECT id FROM passages ORDER BY id")
    ]  # unchanged
    conn.execute("UPDATE bookmarks SET summary = 'rewritten' WHERE id = ?", (read["map1"],))
    conn.commit()
    notebook.prepare(conn, [read["map1"]])
    assert (
        "rewritten"
        in conn.execute(
            "SELECT text FROM passages WHERE bookmark_id = ?", (read["map1"],)
        ).fetchone()[0]
    )


def test_passages_are_found_by_words_and_by_meaning_when_vectors_exist(conn, read, farm):
    ids = list(read.values())
    found, how = notebook.find(
        conn, None, ids, "Which river crosses ten countries to the Black Sea?"
    )
    assert how == "words" and "Danube" in found[0]["text"] and found[0]["title"] == "River maps 0"
    connection = llm.Connection(farm.config(), password="")
    found, how = notebook.find(conn, connection, ids, "sourdough starter flour water feeding")
    assert how == "meaning and words"
    assert any("sourdough starter" in p["text"].lower() for p in found[:3])
    stored = conn.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0]
    assert stored == conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
    calls = len(farm.calls)
    notebook.find(conn, connection, ids, "sourdough again")
    assert len(farm.calls) == calls + 1  # only the question: passages are not embedded twice
    # One long page cannot take every place.
    per_source = {}
    for passage in notebook.find(conn, None, ids, "maps cartography rivers hydrography")[0]:
        per_source[passage["bookmark_id"]] = per_source.get(passage["bookmark_id"], 0) + 1
    assert max(per_source.values()) <= 3
    assert notebook.find(conn, None, [], "anything") == ([], "words")
    assert notebook.find(conn, None, ids, "zzzz qqqq")[0] == []


def test_quoted_words_must_really_be_in_the_passage():
    passage = (
        "The Danube crosses ten countries before it reaches the Black Sea through a wide delta."
    )
    assert notebook.supported("the danube CROSSES ten countries", passage)
    assert notebook.supported("“The Danube crosses ten countries…”", passage)
    assert notebook.supported(
        "The Danube crosses ten countries reaches the Black Sea through a wide", passage
    )
    assert not notebook.supported("The Danube crosses twelve countries", passage)
    assert not notebook.supported(
        "Sea Black the reaches it before countries ten crosses Danube", passage
    )
    assert not notebook.supported("the Danube", passage)  # two words prove nothing
    assert not notebook.supported("", passage)


def test_an_answer_keeps_only_what_the_sources_bear_out(conn, read, farm):
    connection = llm.Connection(farm.config(), password="")
    ids = list(read.values())
    told = notebook.answer(conn, connection, ids, "Which countries does the Danube cross?")
    assert told["found_by"] == "meaning and words" and told["dropped"] == 0
    first = told["statements"][0]
    quoted = told["passages"][first["passage"] - 1]
    assert notebook.supported(first["quote"], quoted["text"])
    assert quoted["bookmark_id"] == read["map0"] and quoted["url"] == "https://example.org/map0"
    sent = farm.calls[-1]["body"]["messages"]
    assert "ONLY the numbered passages" in sent[0]["content"]
    assert "<<<PASSAGES\n[1] from “" in sent[-1]["content"]
    assert sent[-1]["content"].endswith("Question: Which countries does the Danube cross?")
    # A model that makes things up: the invented statements do not get through.
    told = notebook.answer(conn, connection, ids, "Danube countries, and invent something")
    assert told["dropped"] == 2
    assert all("cheese" not in s["text"] for s in told["statements"]) and told["statements"]
    # Nothing in the sources: said so, not guessed.
    told = notebook.answer(conn, connection, ids, "zzzzqqqq wwwwxxxx")
    assert told["statements"] == [] and told["missing"]
    with pytest.raises(ValueError, match="Type a question"):
        notebook.answer(conn, connection, ids, "   ")
    with pytest.raises(ValueError, match="no source"):
        notebook.answer(conn, connection, [], "anything")


def test_a_notebook_holds_sources_a_conversation_and_notes(conn, read, farm):
    connection = llm.Connection(farm.config(), password="")
    with pytest.raises(ValueError, match="Choose some bookmarks"):
        notebook.create(conn, "Empty", ["gone"])
    book = notebook.create(
        conn, "  Rivers   and bread ", [read["map0"], read["cook0"], read["map1"], "gone"]
    )
    assert book["name"] == "Rivers and bread" and len(book["sources"]) == 3
    assert book["depth"] == {"page": 2, "summary": 1}
    first = notebook.ask(conn, connection, book["id"], "Which countries does the Danube cross?")
    assert first["answer"]["statements"] and first["model"] == "gemma4:12b"
    # A follow-up is rewritten with the conversation before it is searched for.
    second = notebook.ask(conn, connection, book["id"], "And where does it end, the delta?")
    assert second["answer"]["searched_for"].startswith(
        "And where does it end, the delta? (about: Which"
    )
    rewrite = next(c for c in farm.calls if "Rewrite the last question" in str(c["body"]))
    assert (
        "user: Which countries does the Danube cross?" in rewrite["body"]["messages"][1]["content"]
    )
    # An unticked source is not read.
    notebook.set_sources(conn, book["id"], use={read["map0"]: False})
    third = notebook.ask(conn, connection, book["id"], "Danube countries Black Sea")
    assert all(p["bookmark_id"] != read["map0"] for p in third["answer"]["passages"])
    shown = notebook.show(conn, book["id"])
    assert [m["role"] for m in shown["messages"]] == ["user", "assistant"] * 3
    assert shown["messages"][0]["text"] == "Which countries does the Danube cross?"
    # Notes: an answer kept with its question as title, and one written by hand.
    notebook.keep(conn, book["id"], message_id=first["id"])
    shown = notebook.keep(conn, book["id"], title="Mine", text="Check the 2019 licences.")
    assert [n["title"] for n in shown["notes"]] == [
        "Which countries does the Danube cross?",
        "Mine",
    ]
    assert shown["notes"][0]["body"]["statements"] and shown["notes"][1]["body"] == {
        "text": "Check the 2019 licences."
    }
    document = notebook.as_markdown(conn, book["id"])
    assert document.startswith("# Rivers and bread\n\n## Which countries does the Danube cross?")
    assert (
        "[1]" in document
        and "  > “" in document
        and "1. [River maps 0](https://example.org/map0)" in document
    )
    assert "Check the 2019 licences." in document
    shown = notebook.drop_note(conn, book["id"], shown["notes"][1]["id"])
    assert len(shown["notes"]) == 1
    with pytest.raises(ValueError, match="empty note"):
        notebook.keep(conn, book["id"], text="  ")
    # Sources come and go; the list shows what each notebook holds.
    shown = notebook.set_sources(
        conn, book["id"], add=[read["cook1"], "gone"], remove=[read["map1"]]
    )
    assert len(shown["sources"]) == 3
    listed = notebook.list_all(conn)
    assert (listed[0]["sources"], listed[0]["questions"], listed[0]["notes"]) == (3, 3, 1)
    assert notebook.rename(conn, book["id"], "Renamed")["name"] == "Renamed"
    assert notebook.clear(conn, book["id"])["messages"] == []
    notebook.delete(conn, book["id"])
    assert notebook.list_all(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM notebook_notes").fetchone()[0] == 0
    with pytest.raises(ValueError, match="no longer there"):
        notebook.show(conn, book["id"])


def test_a_ready_reading_of_the_whole_set_joins_the_conversation(conn, read, farm):
    connection = llm.Connection(farm.config(), password="")
    book = notebook.create(conn, "Set", [read["map0"], read["cook0"]])
    told = notebook.overview(conn, connection, book["id"], "common")
    assert "[1] [2]" in told["answer"]["overview"] and len(told["answer"]["bookmarks"]) == 2
    shown = notebook.show(conn, book["id"])
    assert shown["messages"][0]["text"] == "What do these sources share?"
    assert conn.execute("SELECT COUNT(*) FROM llm_answers").fetchone()[0] == 0
    notebook.keep(conn, book["id"], message_id=told["id"])
    assert "These bookmarks share one thread" in notebook.as_markdown(conn, book["id"])
    with pytest.raises(ValueError, match="Unknown reading"):
        notebook.overview(conn, connection, book["id"], "poem")


def test_through_the_api(path, farm):  # noqa: F811
    conn = db.open_ready(path)
    ids = fill(conn)
    assist.read_pages(conn, limit=40, get=pages({"https://example.org/map0": RIVERS}))
    conn.close()
    app = App(path)
    assert app.notebooks() == {"notebooks": []}
    book = app.notebooks({"action": "create", "name": "Rivers", "ids": [ids["map0"], ids["map1"]]})
    with pytest.raises(RuntimeError, match="No language model is connected"):
        app.notebooks({"action": "ask", "id": book["id"], "question": "Danube?"})
    app.llm_connect(farm.config())
    message = app.notebooks({"action": "ask", "id": book["id"], "question": "Danube countries?"})[
        "message"
    ]
    assert message["answer"]["statements"]
    assert app.notebooks({"action": "overview", "id": book["id"], "kind": "path"})["message"][
        "question"
    ]
    app.notebooks({"action": "keep", "id": book["id"], "message_id": message["id"]})
    assert "Danube" in app.notebooks({"action": "markdown", "id": book["id"]})["markdown"]
    assert app.notebooks({"action": "sources", "id": book["id"], "use": {ids["map1"]: False}})[
        "sources"
    ]
    assert app.notebooks()["notebooks"][0]["questions"] == 2
    assert app.notebooks({"action": "delete", "id": book["id"]}) == {"notebooks": []}
    with pytest.raises(ValueError, match="Unknown notebook action"):
        app.notebooks({"action": "explode"})
    assert json.dumps(message)  # everything returned can go to the page
    app.llm_forget()


def test_the_figures_of_a_set_are_counted_and_offered_as_a_passage(conn, read, farm):
    from atlas.analysis import store

    store.cluster_run(conn, k=2)
    ids = list(read.values())
    seen = notebook.glance(conn, ids)
    assert seen["sources"] == 12 and (seen["first_day"], seen["last_day"]) == (
        "2024-01-01",
        "2024-02-06",
    )
    assert seen["per_year"] == [[2024, 12]] and seen["undated"] == 0
    assert seen["tags"][0] == ["cooking", 6] and seen["sites"] == [["example.org", 12]]
    assert (
        sorted(t["count"] for t in seen["topics"]) == [6, 6]
        and seen["topics"][0]["key"].count(":") == 1
    )
    text = notebook.figures(seen)
    assert text.startswith(
        "This set holds 12 sources.\nTheir dates run from 2024-01-01 to 2024-02-06."
    )
    assert "Sources per year: 2024: 12." in text and "Most used tags: cooking: 6" in text
    assert (
        notebook.glance(conn, [])["sources"] == 0
        and notebook.figures(notebook.glance(conn, [])) == ""
    )
    connection = llm.Connection(farm.config(), password="")
    # A question about the set itself gets the figures as one more passage; another does not.
    told = notebook.answer(conn, connection, ids, "How many sources per year, and which tags?")
    last = told["passages"][-1]
    assert (
        last["figures"]
        and last["bookmark_id"] is None
        and "This set holds 12 sources." in last["text"]
    )
    assert len(told["passages"]) <= notebook.PASSAGES_SHOWN
    assert "Figures counted by Atlas" in farm.calls[-1]["body"]["messages"][-1]["content"]
    told = notebook.answer(conn, connection, ids, "What does the Danube cross?")
    assert not any(p["figures"] for p in told["passages"])
    book = notebook.create(conn, "Set", ids[:4])
    assert notebook.show(conn, book["id"])["glance"]["sources"] == 4


def test_several_connections_are_kept_and_a_question_can_go_to_any(path, tmp_path):  # noqa: F811
    from atlas import settings

    conn = db.open_ready(path)
    ids = fill(conn)
    conn.close()
    first, second = (
        FakeFarm(password="one"),
        FakeFarm(provider="anthropic", password="sk-two", listed=["claude-sonnet-9"]),
    )
    try:
        llm.PROVIDERS["anthropic"] = {**llm.PROVIDERS["anthropic"], "base_url": f"{second.url}/v1"}
        app = App(path)
        app.llm_connect({**first.config(), "password": "one"})
        app.llm_connect(
            {"kind": "anthropic", "chat_model": "claude-sonnet-9", "password": "sk-two"}
        )
        state = app.llm_state()
        assert [(k["slot"], k["active"]) for k in state["kept"]] == [
            ("anthropic", True),
            ("lan", False),
        ]
        assert (
            state["kept"][1]["label"] == "Stand-in farm (gemma4:12b)" and state["kept"][1]["local"]
        )
        assert state["connection"]["slot"] == "anthropic" and state["connection"]["has_password"]
        saved = (tmp_path / "config" / "llm.json").read_text()
        assert "one" not in saved.replace("Stand-in", "") and "sk-two" not in saved
        book = app.notebooks(
            {"action": "create", "name": "Maps", "ids": [ids["map0"], ids["map1"]]}
        )
        ask = {"action": "ask", "id": book["id"], "question": "cartography rivers maps?"}
        assert app.notebooks(ask)["message"]["model"] == "claude-sonnet-9"
        assert second.calls[-1]["path"] == "/v1/messages"
        # The same question sent to the farm, without changing what is in use.
        assert app.notebooks({**ask, "route": "lan"})["message"]["model"] == "gemma4:12b"
        assert first.calls[-1]["path"] == "/v1/chat/completions"
        assert app.llm_state()["connection"]["slot"] == "anthropic"
        with pytest.raises(RuntimeError, match="not kept any more"):
            app.notebooks({**ask, "route": "openai"})
        # Switching back needs no key again; forgetting one leaves the other.
        assert app.llm_use({"slot": "lan"})["connection"]["slot"] == "lan"
        assert app.notebooks(ask)["message"]["model"] == "gemma4:12b"
        state = app.llm_use({"slot": "anthropic", "forget": True})
        assert [k["slot"] for k in state["kept"]] == ["lan"] and state["connection"]["connected"]
        assert settings.get_secret(llm.secret_name("anthropic")) is None
        assert settings.get_secret(llm.secret_name("lan")) == "one"
        app.llm_forget()
        assert app.llm_state()["kept"] == [] and not app.llm_state()["connection"]["connected"]
    finally:
        llm.PROVIDERS["anthropic"] = {
            **llm.PROVIDERS["anthropic"],
            "base_url": "https://api.anthropic.com/v1",
        }
        first.close()
        second.close()


def test_a_connection_file_from_before_is_still_read(tmp_path):
    from atlas import settings

    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "llm.json").write_text(
        '{"kind": "farm", "name": "Old farm", "base_url": "http://10.0.0.5:4000/v1", "chat_model": "m"}'
    )
    settings.set_secret(llm.SECRET, "old-password")
    assert llm.load()["name"] == "Old farm" and llm.describe()["has_password"]
    assert [k["slot"] for k in llm.kept()] == ["lan"]
    assert llm.Connection().password == "old-password"
    llm.activate("lan")  # the key moves to its own place
    assert settings.get_secret(llm.SECRET) is None
    assert settings.get_secret(llm.secret_name("lan")) == "old-password"
    assert llm.Connection().password == "old-password"
