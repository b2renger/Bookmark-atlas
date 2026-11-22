"""No test may reach this computer's real keychain or settings folder."""

import pytest


@pytest.fixture(autouse=True)
def _own_settings(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("ATLAS_NO_KEYRING", "1")
    monkeypatch.delenv("ATLAS_SECRETS_DIR", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setenv("ATLAS_CONFIG_DIR", str(tmp_path_factory.mktemp("settings")))
