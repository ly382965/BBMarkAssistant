import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest


spec = importlib.util.spec_from_file_location(
    "configure_mineru", Path(__file__).resolve().parents[1] / "scripts" / "configure_mineru.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def prepare_runtime(project):
    for relative in (".mineru-venv/Scripts/python.exe", "scripts/mineru_local.py"):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_configure_preserves_settings_backup_and_absolute_paths(tmp_path):
    project = tmp_path / "项目 with spaces"
    prepare_runtime(project)
    app_data = tmp_path / "app data"
    app_data.mkdir()
    path = app_data / "settings.json"
    original = {
        "username": "teacher",
        "grading": {"base_url": "http://localhost:9000/v1", "model": "custom"},
        "rubric": {"max_score": 20, "instructions": "保留评分标准"},
        "custom": {"keep": [1, 2, 3]},
        "ocr": {"mode": "api", "timeout": 3600, "endpoint": "http://localhost:9999/custom", "extra_params": {"lang": "ch"}},
    }
    before = json.dumps(original, ensure_ascii=False).encode("utf-8")
    path.write_bytes(before)
    setup.configure_application(project, [app_data])
    current = json.loads(path.read_text(encoding="utf-8"))
    assert {key: value for key, value in current.items() if key != "ocr"} == {
        key: value for key, value in original.items() if key != "ocr"
    }
    assert current["ocr"]["timeout"] == 3600
    assert current["ocr"]["endpoint"] == original["ocr"]["endpoint"]
    assert current["ocr"]["extra_params"] == original["ocr"]["extra_params"]
    assert current["ocr"]["mode"] == "command"
    command = current["ocr"]["command"]
    assert all(Path(value).is_absolute() for value in command[:2])
    assert command[2:] == ["parse", "{input}", "-o", "{output}/document.md", "--tier", "standard", "--pages", "all", "--ocr-mode", "auto"]
    backups = list(app_data.glob("settings.json.before-mineru-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    setup.configure_application(project, [app_data])
    assert len(list(app_data.glob("*.bak"))) == 1


def test_models_and_custom_target_stay_local(tmp_path, monkeypatch):
    project = tmp_path / "project"
    prepare_runtime(project)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "live-user-data"))
    model_config = setup.configure_models(project)
    text = model_config.read_text(encoding="utf-8")
    assert "source: local" in text and "small_backend: onnx" in text
    assert "engine: llama-cpp" in text and "server_url: ''" in text
    assert (project / ".mineru/models").resolve().as_posix() in text
    test_data = tmp_path / "test-data"
    setup.configure_application(project, [test_data])
    result = json.loads((test_data / "settings.json").read_text(encoding="utf-8"))
    assert result["ocr"]["timeout"] == 1800
    assert result["rubric"]["max_score"] == 10
    assert not (tmp_path / "live-user-data").exists()
    assert not (project / "data").exists()
    assert setup.application_targets(project) == [
        (project / "data").resolve(), (tmp_path / "live-user-data/BBMarkAssistant").resolve()
    ]


def test_failed_atomic_replace_keeps_original_and_backup(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"original")
    with patch.object(setup.os, "replace", side_effect=PermissionError), pytest.raises(PermissionError):
        setup.atomic_replace_with_backup(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert next(tmp_path.glob("*.bak")).read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_second_target_does_not_modify_first_or_echo_secrets(tmp_path):
    prepare_runtime(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    original = b'{"ocr":{"timeout":600},"keep":"secret-test-value"}'
    (first / "settings.json").write_bytes(original)
    (second / "settings.json").write_text("bad-json-secret-test-value", encoding="utf-8")
    with pytest.raises(setup.ConfigurationError) as error:
        setup.configure_application(tmp_path, [first, second])
    assert "secret-test-value" not in str(error.value)
    assert (first / "settings.json").read_bytes() == original
    assert not list(first.glob("*.bak"))


def test_cli_verification_failure_prevents_live_writes_and_redacts_output(capsys):
    with patch.object(setup, "verify_models", side_effect=setup.ConfigurationError("secret-test-value")), \
            patch.object(setup, "configure_application") as configure:
        assert setup.main(["--configure-app"]) == 1
    configure.assert_not_called()
    output = capsys.readouterr()
    assert "secret-test-value" not in output.out + output.err
