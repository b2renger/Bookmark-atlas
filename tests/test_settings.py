"""Secrets: the keychain when there is one, never the data folder."""

import json
import types

import pytest

from atlas import llm, settings


class FakeKeychain:
    """Stands in for the ``keyring`` module."""

    def __init__(self, refuses=False):
        self.kept, self.refuses = {}, refuses

    def get_password(self, service, user):
        return self.kept.get((service, user))

    def set_password(self, service, user, value):
        if self.refuses:
            raise RuntimeError("the keychain is locked")
        self.kept[(service, user)] = value

    def delete_password(self, service, user):
        del self.kept[(service, user)]


@pytest.fixture
def folders(tmp_path, monkeypatch):
    data, private = tmp_path / "data", tmp_path / "private"
    data.mkdir()
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(data))
    monkeypatch.setenv("ATLAS_SECRETS_DIR", str(private))
    return types.SimpleNamespace(data=data, private=private)


def use(monkeypatch, keychain):
    monkeypatch.setattr(settings, "_keyring", lambda: keychain)


def test_without_a_keychain_the_file_is_not_in_the_data_folder(folders):
    assert settings.set_token("ntn_abcdefghijklmnop") == "file"
    assert settings.set_secret("llm-key:openai", "sk-test") == "file"
    assert not (folders.data / "secrets.json").exists()
    assert json.loads((folders.private / "secrets.json").read_text()) == {
        "notion_token": "ntn_abcdefghijklmnop",
        "llm-key:openai": "sk-test",
    }
    assert settings.get_token() == "ntn_abcdefghijklmnop" and settings.kept_in() == "file"
    settings.delete_token()
    settings.delete_secret("llm-key:openai")
    assert not (folders.private / "secrets.json").exists()  # an empty file is not left behind


def test_with_a_keychain_nothing_is_written_to_a_file(folders, monkeypatch):
    keychain = FakeKeychain()
    use(monkeypatch, keychain)
    assert settings.set_token("ntn_abcdefghijklmnop") == "keychain"
    assert settings.set_secret("llm-key:lan", "pw") == "keychain"
    assert (
        not (folders.data / "secrets.json").exists()
        and not (folders.private / "secrets.json").exists()
    )
    assert settings.token_source() == "keychain" and settings.get_secret("llm-key:lan") == "pw"
    assert settings.kept_in() == "keychain"
    settings.delete_secret("llm-key:lan")
    assert settings.get_secret("llm-key:lan") is None


def test_a_file_left_in_the_data_folder_moves_to_the_keychain(folders, monkeypatch):
    (folders.data / "secrets.json").write_text('{"notion_token": "ntn_old", "llm-key": "farm-pw"}')
    keychain = FakeKeychain()
    use(monkeypatch, keychain)
    assert settings.get_token() == "ntn_old" and settings.get_secret("llm-key") == "farm-pw"
    assert not (folders.data / "secrets.json").exists()
    assert keychain.kept == {
        ("bookmark-atlas", "notion-token"): "ntn_old",
        ("bookmark-atlas", "llm-key"): "farm-pw",
    }


def test_a_file_left_in_the_data_folder_moves_to_the_private_folder(folders):
    (folders.data / "secrets.json").write_text('{"notion_token": "ntn_old"}')
    assert settings.settle() == 1 and settings.settle() == 0
    assert not (folders.data / "secrets.json").exists()
    assert json.loads((folders.private / "secrets.json").read_text()) == {"notion_token": "ntn_old"}
    assert settings.token_source() == "file"


def test_a_folder_from_someone_else_cannot_replace_your_keys(folders, monkeypatch):
    keychain = FakeKeychain()
    use(monkeypatch, keychain)
    settings.set_token("ntn_mine")
    (folders.data / "secrets.json").write_text('{"notion_token": "ntn_theirs"}')
    assert settings.get_token() == "ntn_mine"
    assert not (folders.data / "secrets.json").exists()  # and their file does not stay in clear
    monkeypatch.setattr(settings, "_keyring", lambda: None)
    settings.set_secret("llm-key:lan", "mine")
    (folders.data / "secrets.json").write_text('{"llm-key:lan": "theirs"}')
    assert settings.get_secret("llm-key:lan") == "mine"


def test_a_keychain_that_refuses_falls_back_to_the_private_file(folders, monkeypatch):
    use(monkeypatch, FakeKeychain(refuses=True))
    assert settings.set_secret("llm-key:lan", "pw") == "file"
    assert settings.get_secret("llm-key:lan") == "pw"
    assert (folders.private / "secrets.json").exists() and not (
        folders.data / "secrets.json"
    ).exists()
    # Once the keychain answers, the file is emptied into it.
    keychain = FakeKeychain()
    use(monkeypatch, keychain)
    assert settings.get_secret("llm-key:lan") == "pw"
    assert keychain.kept == {("bookmark-atlas", "llm-key:lan"): "pw"}
    assert not (folders.private / "secrets.json").exists()


def test_connections_are_kept_beside_the_keys_not_with_the_data(folders):
    llm.save({"kind": "server", "base_url": "http://10.0.0.5:4000/v1", "chat_model": "m"})
    assert (folders.private / "llm.json").exists() and not (folders.data / "llm.json").exists()


def test_connections_saved_in_the_data_folder_by_an_older_version_are_adopted_once(folders):
    old = {
        "kind": "farm",
        "name": "Old farm",
        "base_url": "http://10.0.0.5:4000/v1",
        "chat_model": "m",
    }
    (folders.data / "llm.json").write_text(json.dumps(old))
    assert llm.load()["name"] == "Old farm"
    assert (folders.private / "llm.json").exists() and not (folders.data / "llm.json").exists()
    # A data folder opened later cannot redirect the connections of this computer.
    (folders.data / "llm.json").write_text(
        json.dumps({**old, "base_url": "http://evil.example/v1"})
    )
    assert llm.load()["base_url"] == "http://10.0.0.5:4000/v1"
