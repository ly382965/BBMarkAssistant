import copy
import json
from unittest.mock import patch

import pytest

from bb_assistant.settings import DEFAULTS, Settings


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    monkeypatch.setattr("bb_assistant.mineru_runtime.discover_local_command", lambda *args, **kwargs: None)
    for name in ("BBMARK_GPT_API_KEY", "BBMARK_DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_old_provider_config_preserved_and_new_sections_defaulted(tmp_path):
    data = copy.deepcopy(DEFAULTS)
    data.pop("gpt")
    data.pop("recognition")
    data["grading"].update(base_url="https://custom.example/v1", model="private-grader")
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.data["grading"]["base_url"] == "https://custom.example/v1"
    assert settings.data["grading"]["model"] == "private-grader"
    assert settings.data["gpt"]["wire_api"] == "responses"
    assert settings.data["recognition"]["mode"] == "auto"


@pytest.mark.parametrize("provider,variable", [("gpt", "BBMARK_GPT_API_KEY"), ("deepseek", "BBMARK_DEEPSEEK_API_KEY")])
def test_named_environment_overrides_memory_and_vault_without_persistence(tmp_path, monkeypatch, provider, variable):
    settings = Settings(tmp_path)
    settings._secrets[provider] = "in-memory-fixture"
    monkeypatch.setenv(variable, "  environment-fixture  ")
    with patch("keyring.get_password", return_value="vault-fixture") as vault:
        assert settings.secret(provider) == "environment-fixture"
        assert settings.secret(provider, include_environment=False) == "in-memory-fixture"
        vault.assert_not_called()
    settings.save(settings.data)
    assert "environment-fixture" not in settings.path.read_text(encoding="utf-8")


def test_generic_openai_environment_not_used_for_gpt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "codex-fixture")
    with patch("keyring.get_password", return_value="vault-fixture"):
        assert Settings(tmp_path).secret("gpt") == "vault-fixture"


@pytest.mark.parametrize("unsafe_url", ["https://user:secret@example.org/v1", "https://example.org/v1?api_key=secret"])
def test_gpt_url_rejects_embedded_credentials(tmp_path, unsafe_url):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data["gpt"]["base_url"] = unsafe_url
    with pytest.raises(ValueError):
        settings.save(data)
    assert not settings.path.exists()


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key", "Cookie", "Host", "Content-Length"])
def test_gpt_advanced_headers_reject_auth_and_transport_overrides(tmp_path, header):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data["gpt"]["http_headers"] = {header: "fixture"}
    with pytest.raises(ValueError):
        settings.save(data)


def test_gpt_nonsecret_routing_header_and_provider_protocol_roundtrip(tmp_path):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data["gpt"]["http_headers"] = {"x-openai-actor-authorization": "local-image-extension"}
    data["recognition"]["mode"] = "vision"
    settings.save(data)
    assert Settings(tmp_path).data == data


@pytest.mark.parametrize("section,key,value", [("gpt", "wire_api", "invalid"), ("recognition", "mode", "invalid")])
def test_invalid_routes_and_wire_apis_rejected(tmp_path, section, key, value):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data[section][key] = value
    with pytest.raises(ValueError):
        settings.save(data)
