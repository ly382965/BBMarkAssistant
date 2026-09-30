import copy
from unittest.mock import patch

import keyring.errors
import pytest

from bb_assistant.settings import DEFAULTS, Settings


@pytest.fixture(autouse=True)
def isolate_installed_mineru(monkeypatch):
    monkeypatch.setattr("bb_assistant.mineru_runtime.discover_local_command", lambda *args, **kwargs: None)


def test_stale_in_memory_command_cannot_undo_installer(tmp_path, monkeypatch):
    import json
    config = Settings(tmp_path)
    stale = copy.deepcopy(config.data)
    local = ["C:/local/python.exe", "C:/local/scripts/mineru_local.py", "parse", "{input}", "-o", "{output}/document.md"]
    monkeypatch.setattr("bb_assistant.mineru_runtime.discover_local_command", lambda *args, **kwargs: local)
    config.save(stale)
    # The installed launcher supplies the path; the user's explicit tier survives repair.
    expected = local + ["--tier", "advanced"]
    assert config.data["ocr"]["command"] == expected
    assert json.loads(config.path.read_text(encoding="utf-8"))["ocr"]["command"] == expected
    assert config.data["grading"] == stale["grading"]
    assert config.data["rubric"] == stale["rubric"]


def test_settings_do_not_write_secrets(tmp_path):
    config = Settings(tmp_path)
    data = copy.deepcopy(DEFAULTS)
    data["grading"]["api_key"] = "test-not-real"
    with pytest.raises(ValueError, match="凭据"):
        config.save(data)
    assert not config.path.exists()


def test_course_identity_is_derived_from_https_url(tmp_path):
    config = Settings(tmp_path)
    config.save(copy.deepcopy(DEFAULTS))
    assert Settings(tmp_path).data["bb"]["course_id"] == "_12345_1"
    bad = copy.deepcopy(DEFAULTS)
    bad["course_url"] = "http://example.org/?course_id=_1_1"
    with pytest.raises(ValueError, match="HTTPS"):
        config.save(bad)


@pytest.mark.parametrize("field", ["course_url", "bb.base_url", "bb.cas_service", "ocr.endpoint", "grading.base_url"])
@pytest.mark.parametrize("unsafe_url", [
    "https://teacher:example-secret@example.org/?course_id=_1_1",
    "https://example.org/?course_id=_1_1&api_key=example-secret",
    "https://example.org/?course_id=_1_1&ACCESS-TOKEN=example-secret",
    "https://example.org/?course_id=_1_1&%61piKey=example-secret",
    "https://example.org/?course_id=_1_1#access_token=example-secret",
])
def test_connection_url_credentials_are_rejected_before_persistence(tmp_path, field, unsafe_url):
    settings = Settings(tmp_path)
    settings.save(copy.deepcopy(DEFAULTS))
    before = settings.path.read_bytes()
    data = copy.deepcopy(settings.data)
    if "." in field:
        section, name = field.split(".")
        data[section][name] = unsafe_url
    else:
        data[field] = unsafe_url
    with pytest.raises(ValueError) as error:
        settings.save(data)
    assert "example-secret" not in str(error.value)
    assert settings.path.read_bytes() == before
    assert not settings.path.with_suffix(".tmp").exists()
    assert settings.data == DEFAULTS


def test_http_cas_service_and_local_ocr_are_valid_without_inspecting_rubric_urls(tmp_path):
    settings = Settings(tmp_path)
    data = copy.deepcopy(DEFAULTS)
    data["bb"]["cas_service"] = "http://www.bb.ustc.edu.cn/nginx_auth/login.php?next=6874747073"
    data["rubric"]["reference_answer"] = "请解释这个示例为何不安全：https://example.org/?api_key=example-secret"
    settings.save(data)
    assert settings.data["bb"]["cas_service"] == data["bb"]["cas_service"]
    assert settings.data["rubric"]["reference_answer"] == data["rubric"]["reference_answer"]


def test_no_keyring_allows_explicit_memory_only_secret(tmp_path):
    settings = Settings(tmp_path)
    with patch("keyring.delete_password", side_effect=keyring.errors.NoKeyringError("no backend")):
        settings.set_secret("deepseek", "memory-only-test-key", remember=False)
    assert settings.secret("deepseek") == "memory-only-test-key"
    assert not settings.path.exists()


@pytest.mark.parametrize("error", [keyring.errors.NoKeyringError("no backend"), RuntimeError("vault failed")])
def test_requested_persistence_failure_stays_visible(tmp_path, error):
    settings = Settings(tmp_path)
    with patch("keyring.set_password", side_effect=error), pytest.raises(type(error)):
        settings.set_secret("deepseek", "test-key", remember=True)


def test_memory_only_does_not_swallow_unexpected_vault_failure(tmp_path):
    settings = Settings(tmp_path)
    with patch("keyring.delete_password", side_effect=RuntimeError("vault failed")), pytest.raises(RuntimeError):
        settings.set_secret("deepseek", "test-key", remember=False)


def test_absent_saved_secret_is_not_an_error(tmp_path):
    settings = Settings(tmp_path)
    with patch("keyring.delete_password", side_effect=keyring.errors.PasswordDeleteError("absent")):
        settings.set_secret("deepseek", "test-key", remember=False)
    assert settings.secret("deepseek") == "test-key"
