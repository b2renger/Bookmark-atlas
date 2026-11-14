"""The connection to a language model, against a stand-in farm over real HTTP."""

import json
import socket
import threading
import time

import pytest

from atlas import llm, settings
from fake_farm import FakeFarm


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")


@pytest.fixture
def farm():
    made = FakeFarm()
    yield made
    made.close()


def test_a_farm_describes_itself_and_atlas_uses_the_address_it_was_reached_at(farm):
    card = llm.read_self("127.0.0.1", farm.port)
    assert card["name"] == "Stand-in farm" and card["chat_model"] == "gemma4:12b"
    # The farm believes it is 10.9.9.9; what works is the address that answered.
    assert card["base_url"] == f"http://127.0.0.1:{farm.port}/v1"
    assert card["embeddings"] and not card["requires_password"]
    with pytest.raises(llm.LlmError, match="did not answer"):
        llm.read_self("127.0.0.1", 9)


def test_farms_announcing_themselves_are_heard(farm):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def announce():
        time.sleep(0.2)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
            out.sendto(b"not json", ("127.0.0.1", port))
            out.sendto(json.dumps({"hello": 1}).encode(), ("127.0.0.1", port))
            out.sendto(json.dumps(farm.snapshot()).encode(), ("127.0.0.1", port))

    threading.Thread(target=announce).start()
    found = llm.discover(seconds=1.0, port=port)
    assert [card["name"] for card in found] == ["Stand-in farm"]
    assert found[0]["base_url"] == f"http://127.0.0.1:{farm.port}/v1"


def test_test_then_save_and_the_password_never_reaches_the_settings_file(tmp_path):
    farm = FakeFarm(password="s3cret")
    try:
        with pytest.raises(llm.LlmError, match="did not accept the password"):
            llm.test(farm.config(), "wrong")
        result = llm.test(farm.config(), "s3cret")
        assert result["reply"] == "ready" and result["chat_model"] == "gemma4:12b"
        assert result["vectors"] == {"model": "embeddinggemma-2", "dims": 48}
        llm.save({**farm.config(), "password": "s3cret", "junk": 1})
        settings.set_secret(llm.SECRET, "s3cret")
        saved = (tmp_path / "config" / "llm.json").read_text()
        assert "s3cret" not in saved and "junk" not in saved
        shown = llm.describe()
        assert shown["connected"] and shown["has_password"] and shown["local"]
        assert "s3cret" not in json.dumps(shown)
        assert llm.Connection().chat([{"role": "user", "content": "single word: ready"}]) == "ready"
        llm.forget()
        assert not llm.describe()["connected"] and settings.get_secret(llm.SECRET) is None
        with pytest.raises(llm.LlmError, match="No language model is connected"):
            llm.Connection()
    finally:
        farm.close()


def test_a_full_farm_is_waited_for_briefly_then_reported(farm):
    connection = llm.Connection(farm.config(), password="")
    farm.busy = 1
    assert connection.chat([{"role": "user", "content": "single word: ready"}]) == "ready"
    farm.busy, farm.retry_after = 5, 600
    with pytest.raises(llm.Busy) as caught:
        connection.chat([{"role": "user", "content": "single word: ready"}])
    assert caught.value.retry_after == 600 and "seats" in str(caught.value)


def test_json_is_asked_for_without_thinking_and_read_however_it_is_wrapped(farm):
    connection = llm.Connection(farm.config(), password="")
    messages = [
        {"role": "system", "content": "You name the topics"},
        {"role": "user", "content": "Topic 0 (3 bookmarks)\n  tags: maps, data"},
    ]
    assert connection.ask_json(messages) == {"names": {"0": "About maps"}}
    sent = farm.calls[-1]["body"]
    assert sent["response_format"] == {"type": "json_object"} and sent["think"] is False
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and sent["stream"] is False
    # A server that refuses response_format is asked again without it, and remembered.
    farm.json_mode = False
    assert connection.ask_json(messages) == {"names": {"0": "About maps"}}
    assert "response_format" not in farm.calls[-1]["body"]
    before = len(farm.calls)
    connection.ask_json(messages)
    assert len(farm.calls) == before + 1


def test_parse_json_and_thinking():
    assert llm.parse_json('Sure!\n```json\n{"a": [1, 2]}\n```\nDone.') == {"a": [1, 2]}
    assert llm.parse_json('<think>{"no": 1}</think>{"a": 1}') == {"a": 1}
    assert llm.parse_json("reasoning…</think>\n[1, 2]") == [1, 2]
    assert llm.parse_json('Here: {"a": "b"} I hope it helps') == {"a": "b"}
    with pytest.raises(llm.LlmError):
        llm.parse_json("no json here")


def test_vectors_come_back_in_order_with_the_prefixes_the_model_expects(farm):
    connection = llm.Connection(farm.config(), password="")
    where = connection.embedding()
    assert where["url"] == f"http://127.0.0.1:{farm.port}/v1/embeddings"
    assert where["contract"] == "embeddinggemma-2/768/v1" and where["key"] == "open-embed-key"
    texts = [
        llm.document_text(where["contract"], f"Title {i}", f"body {i} words") for i in range(40)
    ]
    assert texts[0] == "title: Title 0 | text: body 0 words"
    assert llm.document_text(where["contract"], None, "x") == "title: none | text: x"
    assert llm.query_text(where["contract"], "maps") == "task: search result | query: maps"
    vectors = connection.embed_many(texts, batch=16)
    from fake_farm import vector

    assert len(vectors) == 40 and vectors[17] == vector(texts[17])


def test_a_farm_with_a_password_hands_its_vector_key_to_who_holds_it():
    farm = FakeFarm(password="s3cret")
    try:
        connection = llm.Connection(farm.config(), password="s3cret")
        assert connection.embedding()["key"] == "embed-key"
        assert len(connection.embed(["one"])) == 1
        with pytest.raises(llm.LlmError, match="password"):
            llm.Connection(farm.config(), password="nope").embedding()
    finally:
        farm.close()


def test_a_farm_without_document_search_and_a_plain_server():
    farm = FakeFarm(embeddings=False)
    try:
        with pytest.raises(llm.LlmError, match="Document search"):
            llm.Connection(farm.config(), password="").embedding()
        assert "Document search" in llm.test(farm.config(), "")["vectors_note"]
        plain = {"base_url": f"{farm.url}/v1", "chat_model": "gemma4:12b"}
        with pytest.raises(llm.LlmError, match="No model for text vectors"):
            llm.Connection(plain, password="").embedding()
        named = llm.Connection({**plain, "embed_model": "nomic-embed"}, password="k").embedding()
        assert named["url"] == f"{farm.url}/v1/embeddings" and named["contract"] == "nomic-embed/v1"
    finally:
        farm.close()


def test_which_addresses_stay_on_the_network():
    for local in (
        "http://127.0.0.1:4000/v1",
        "http://192.168.1.20:4000",
        "http://10.0.0.5/v1",
        "http://172.20.1.1",
        "http://localhost:11434/v1",
        "http://gpubox:4000",
        "http://farm.local:4000",
    ):
        assert llm.is_local(local), local
    for away in ("https://api.openai.com/v1", "http://8.8.8.8", "http://172.40.0.1", ""):
        assert not llm.is_local(away), away
