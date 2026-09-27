from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

from bb_assistant import mineru_runtime as runtime
from bb_assistant.services import OcrClient, OcrError


def installation(root: Path) -> list[str]:
    for relative in (".mineru-venv/Scripts/python.exe", "scripts/mineru_local.py", ".mineru/config.yaml"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    return runtime.discover_local_command([root])


def test_stock_missing_command_is_repaired_without_mutating_config(tmp_path, monkeypatch):
    local = installation(tmp_path)
    monkeypatch.setattr(runtime.shutil, "which", lambda value: None)
    config = {"mode": "command", "command": copy.deepcopy(runtime.DEFAULT_MINERU_COMMAND), "timeout": 1800}
    before = copy.deepcopy(config)
    result = runtime.resolve_ocr_config(config, search_roots=[tmp_path])
    assert result["command"] == local
    assert result["timeout"] == 1800
    assert config == before
    assert runtime.validate_ocr_command(config, search_roots=[tmp_path]) == local


def test_frozen_bundle_finds_runtime_at_project_ancestor(tmp_path, monkeypatch):
    expected = installation(tmp_path)
    executable = tmp_path / "dist" / "BBMarkAssistant" / "BBMarkAssistant.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(runtime, "__file__", str(executable.parent / "_internal" / "bb_assistant" / "mineru_runtime.py"))
    assert runtime.discover_local_command() == expected


def test_missing_or_moved_local_wrapper_is_repaired(tmp_path):
    expected = installation(tmp_path / "moved")
    command = [str(tmp_path / "old" / ".mineru-venv" / "Scripts" / "python.exe"),
               str(tmp_path / "old" / "scripts" / "mineru_local.py"), "parse", "{input}", "-o", "{output}"]
    assert runtime.resolve_ocr_config({"command": command}, search_roots=[tmp_path / "moved"])["command"] == expected


def test_existing_python_with_missing_wrapper_is_repaired(tmp_path):
    expected = installation(tmp_path)
    command = [str(Path(sys.executable).resolve()), str(tmp_path / "gone" / "mineru_local.py"), "{input}", "{output}"]
    assert runtime.resolve_ocr_config({"command": command}, search_roots=[tmp_path])["command"] == expected


@pytest.mark.parametrize("mode", ["http", "mineru_v1"])
def test_api_modes_unchanged_even_with_local_installation(tmp_path, mode):
    installation(tmp_path)
    config = {"mode": mode, "command": runtime.DEFAULT_MINERU_COMMAND, "endpoint": "https://ocr.example.test"}
    assert runtime.resolve_ocr_config(config, search_roots=[tmp_path]) == config
    assert runtime.validate_ocr_command(config, search_roots=[tmp_path]) == []


def test_valid_custom_command_is_preserved(tmp_path):
    installation(tmp_path)
    executable = tmp_path / "custom-ocr.exe"
    executable.write_bytes(b"fixture")
    config = {"command": [str(executable), "--input", "{input}", "--dest", "{output}"], "timeout": 23}
    assert runtime.resolve_ocr_config(config, search_roots=[tmp_path]) == config
    assert runtime.validate_ocr_command(config, search_roots=[tmp_path]) == config["command"]


def test_available_stock_executable_is_preserved(tmp_path, monkeypatch):
    installation(tmp_path)
    monkeypatch.setattr(runtime.shutil, "which", lambda value: "C:/known/mineru-kit.exe")
    config = {"command": ["mineru-kit", "parse", "{input}", "-o", "{output}", "--tier", "fast"]}
    assert runtime.resolve_ocr_config(config, search_roots=[tmp_path]) == config


def test_missing_custom_executable_is_not_replaced(tmp_path):
    installation(tmp_path)
    config = {"command": [str(tmp_path / "missing-custom.exe"), "{input}", "{output}"]}
    assert runtime.resolve_ocr_config(config, search_roots=[tmp_path]) == config
    with pytest.raises(ValueError, match="可执行文件不存在"):
        runtime.validate_ocr_command(config, search_roots=[tmp_path])


def test_incomplete_installation_is_not_discovered(tmp_path):
    installation(tmp_path)
    (tmp_path / ".mineru" / "config.yaml").unlink()
    assert runtime.discover_local_command([tmp_path]) is None


def test_preflight_reports_missing_custom_command_without_running_process(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not launch OCR")

    monkeypatch.setattr("bb_assistant.services.subprocess.run", forbidden)
    client = OcrClient({"command": [str(tmp_path / "custom-missing.exe"), "{input}", "{output}"]})
    with pytest.raises(OcrError, match="可执行文件不存在"):
        client.preflight()
