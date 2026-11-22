"""The model inside the app: downloads, and llama-server run as a real program
(a stand-in that takes the same arguments)."""

import hashlib
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from atlas import db, engine, llm, notebook
from atlas.server import App
from test_assist import fill
from test_clusters import path  # noqa: F401


@pytest.fixture(autouse=True)
def own_places(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")
    monkeypatch.setenv("ATLAS_ENGINE_DIR", str(tmp_path / "engine"))
    monkeypatch.setenv("ATLAS_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("LLAMA_LOG_PROMPTS", "1")  # must not reach the engine
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    engine._downloads.clear()
    yield
    engine.stop()
    engine._downloads.clear()


def install_engine(tmp_path):
    """Put the stand-in where the app looks for llama-server."""
    folder = tmp_path / "engine"
    folder.mkdir(exist_ok=True)
    stand_in = Path(__file__).with_name("fake_llama_server.py")
    if sys.platform.startswith("win"):
        pytest.skip("the stand-in is started through a shebang")
    program = folder / "llama-server"
    program.write_text(
        f"#!{sys.executable}\n"
        + stand_in.read_text().replace(
            "os.path.dirname(os.path.abspath(__file__))", repr(str(stand_in.parent))
        )
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return program


def have(key):
    target = engine.model_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"gguf")


class Reply:
    def __init__(self, data, status=200, chunk=None):
        self.data, self.status, self.at, self.chunk = data, status, 0, chunk
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size):
        size = min(size, self.chunk or size)
        block = self.data[self.at : self.at + size]
        self.at += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def wait_for(key, *statuses, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if engine._downloads[key]["status"] in statuses:
            return engine._downloads[key]
        time.sleep(0.02)
    raise AssertionError(engine._downloads[key])


def test_what_the_page_is_told(tmp_path):
    state = engine.state()
    assert state["available"] is False and state["advised"] in engine.WRITERS
    assert [m["key"] for m in state["models"]] == ["light", "medium", "advanced", "vectors"]
    assert all(not m["downloaded"] and m["download"] is None for m in state["models"])
    assert state["models"][1]["gigabytes"] == 3.1 and state["models"][1]["memory"] == 8
    install_engine(tmp_path)
    have("medium")
    state = engine.state()
    assert state["available"] and state["shipped"] and state["models"][1]["downloaded"]
    # Every file comes from a named place over https; the vectors file is pinned by its hash.
    for entry in engine.CATALOGUE.values():
        assert entry["url"].startswith("https://huggingface.co/") and entry["url"].endswith(
            entry["file"]
        )
    assert len(engine.CATALOGUE["vectors"]["sha256"]) == 64


def test_a_download_is_checked_and_can_resume(monkeypatch):
    data = b"model bytes " * 1000
    monkeypatch.setitem(
        engine.CATALOGUE,
        "vectors",
        {
            **engine.CATALOGUE["vectors"],
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    seen = []

    def opener(request, timeout=0):
        seen.append(request.headers.get("Range"))
        start = int(request.headers["Range"][6:-1]) if request.headers.get("Range") else 0
        return Reply(data[start:], status=206 if start else 200)

    # A part left by an earlier try is continued, not fetched again.
    part = engine.model_path("vectors").with_name("embeddinggemma-2-BF16.gguf.part")
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:5000])
    engine.download("vectors", opener)
    done = wait_for("vectors", "done", "failed")
    assert done["status"] == "done" and seen == ["bytes=5000-"]
    assert engine.model_path("vectors").read_bytes() == data and not part.exists()
    assert engine.state()["models"][3]["downloaded"]
    assert engine.download("vectors", opener)["models"][3]["downloaded"] and len(seen) == 1
    # The wrong bytes are thrown away.
    engine.remove("vectors")
    engine.download("vectors", lambda request, timeout=0: Reply(b"x" * len(data)))
    failed = wait_for("vectors", "done", "failed")
    assert failed["status"] == "failed" and "not the expected one" in failed["error"]
    assert not engine.model_path("vectors").exists() and not part.exists()


def test_a_download_can_fail_or_be_cancelled_and_says_so(monkeypatch):
    monkeypatch.setitem(engine.CATALOGUE, "light", {**engine.CATALOGUE["light"], "bytes": 4000})

    def broken(request, timeout=0):
        raise OSError("network unreachable")

    engine.download("light", broken)
    failed = wait_for("light", "failed")
    assert "continues where it stopped" in failed["error"]
    engine.download("light", lambda request, timeout=0: Reply(b"y" * 100))  # far too short
    assert "stopped early" in wait_for("light", "failed")["error"]
    engine.remove("light")

    class Slow(Reply):
        def read(self, size):
            time.sleep(0.05)
            return super().read(size)

    engine.download("light", lambda request, timeout=0: Slow(b"z" * 4000, chunk=10))
    time.sleep(0.15)
    engine.cancel("light")
    assert wait_for("light", "cancelled")["status"] == "cancelled"
    assert not engine.model_path("light").exists()
    with pytest.raises(engine.EngineError, match="Unknown model"):
        engine.download("huge")
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda where: type("U", (), {"free": 10})())
    with pytest.raises(engine.EngineError, match="Not enough room"):
        engine.download("advanced")


def test_the_engine_runs_as_a_program_and_answers(tmp_path, monkeypatch):
    with pytest.raises(engine.EngineError, match="not downloaded yet"):
        engine.ensure("medium")
    have("medium")
    with pytest.raises(engine.EngineError, match="not in this copy of the app"):
        engine.ensure("medium")
    install_engine(tmp_path)
    server = engine.ensure("medium", wait=20)
    assert server["url"].startswith("http://127.0.0.1:") and len(server["key"]) > 20
    assert engine.ensure("medium")["process"] is server["process"]  # not started twice
    assert engine.state()["models"][1]["running"]
    # Through the ordinary connection: started on first use, spoken to with its key.
    connection = llm.Connection(
        {"kind": "local", "base_url": "local", "chat_model": "medium"}, password=""
    )
    assert connection.chat([{"role": "user", "content": "single word: ready"}]) == "ready"
    assert connection.chat_model == "Gemma 4 E2B" and connection.models() == ["medium"]
    assert (
        llm.Connection(
            {"kind": "local", "base_url": "local", "chat_model": "medium"}, password=""
        ).api
        == "local"
    )
    # Vectors need their own file; until then a clear refusal that callers can fall back on.
    with pytest.raises(llm.LlmError, match="not downloaded yet"):
        connection.embedding()
    have("vectors")
    where = connection.embedding()
    assert where["contract"] == "embeddinggemma-2/768/v1" == engine.VECTOR_CONTRACT
    assert len(connection.embed(["one", "two"])) == 2
    assert {role for role in engine._servers} == {"writer", "vectors"}
    # Another size replaces the one running.
    have("light")
    first = server["process"]
    engine.ensure("light", wait=20)
    assert first.poll() is not None and engine.state()["models"][0]["running"]
    engine.stop()
    assert engine._servers == {} and not engine.state()["models"][0]["running"]


def test_an_engine_that_cannot_load_says_why(tmp_path, monkeypatch):
    install_engine(tmp_path)
    have("medium")
    monkeypatch.setenv("FAKE_LLAMA_CRASH", "1")
    with pytest.raises(engine.EngineError, match="stopped while loading"):
        engine.ensure("medium", wait=10)
    monkeypatch.delenv("FAKE_LLAMA_CRASH")
    monkeypatch.setenv("FAKE_LLAMA_LOAD", "30")
    with pytest.raises(engine.EngineError, match="too long to load"):
        engine.ensure("medium", wait=1.5)
    assert engine._servers == {}


def test_the_form_of_an_answer_is_imposed_on_a_built_in_model(tmp_path):
    install_engine(tmp_path)
    have("medium")
    connection = llm.Connection(
        {"kind": "local", "base_url": "local", "chat_model": "medium"}, password=""
    )
    sent = {}
    real = llm.request

    def spy(method, url, **options):
        sent.update(options.get("body") or {}, timeout=options.get("timeout"))
        return real(method, url, **options)

    llm.request, keep = spy, llm.request
    try:
        connection.ask_json(
            [
                {"role": "system", "content": "You name the topics"},
                {"role": "user", "content": "Topic 0 (1 bookmarks)\n  tags: maps"},
            ],
            schema=notebook.ANSWER_SHAPE,
        )
    finally:
        llm.request = keep
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] is notebook.ANSWER_SHAPE
    assert sent["timeout"] == 900


def test_through_the_api_download_use_ask_remove(tmp_path, path):  # noqa: F811
    conn = db.open_ready(path)
    ids = fill(conn)
    conn.close()
    install_engine(tmp_path)
    app = App(path)
    assert app.engine()["available"] and app.llm_state()["connection"]["connected"] is False
    with pytest.raises(ValueError, match="three sizes"):
        app.llm_connect({"kind": "local", "chat_model": "vectors"})
    have("medium")
    state = app.llm_connect({"kind": "local", "chat_model": "medium"})["connection"]
    assert state["kind"] == "local" and state["local"] and state["name"] == "This computer"
    tried = app.llm_test({"kind": "local", "chat_model": "medium"})
    assert tried["reply"] == "ready" and "not downloaded yet" in tried["vectors_note"]
    book = app.notebooks({"action": "create", "name": "Maps", "ids": [ids["map0"], ids["map1"]]})
    message = app.notebooks(
        {"action": "ask", "id": book["id"], "question": "cartography rivers maps?"}
    )
    assert message["message"]["answer"]["found_by"] == "words"  # no vectors file yet
    assert message["message"]["answer"]["statements"]
    have("vectors")
    message = app.notebooks({"action": "ask", "id": book["id"], "question": "hydrography maps?"})
    assert message["message"]["answer"]["found_by"] == "meaning and words"
    assert app.llm_vectors({"limit": 50})["computed"] == 12
    # Removing the model in use disconnects; stopping leaves it connected.
    assert app.engine({"action": "stop"})["models"][1]["running"] is False
    assert app.engine({"action": "remove", "model": "medium"})["models"][1]["downloaded"] is False
    assert app.llm_state()["connection"]["connected"] is False
    with pytest.raises(ValueError, match="Unknown action"):
        app.engine({"action": "explode"})
    assert os.environ.get("LLAMA_LOG_PROMPTS") == "1"
