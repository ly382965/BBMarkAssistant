from __future__ import annotations

import copy
import json
import sys

import pytest

from bb_assistant.app import MainWindow
from bb_assistant.mineru_runtime import mineru_command_ocr_mode, mineru_command_tier


@pytest.fixture
def window(qtbot, tmp_path):
    window = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(window)
    return window


def local_command(tmp_path, *options):
    wrapper = tmp_path / "mineru_local.py"
    wrapper.write_text("# test fixture, never executed", encoding="utf-8")
    return [sys.executable, str(wrapper), "parse", "{input}", "-o", "{output}/document.md", *options]


def test_tier_selector_updates_saved_command_and_survives_restart(window, tmp_path, qtbot):
    command = local_command(tmp_path, "--tier=standard", "--pages", "all", "--ocr-mode", "auto")
    before_grading = copy.deepcopy(window.settings.data["grading"])
    window.ocr_command.setPlainText(json.dumps(command))
    assert window.ocr_tier.currentData() == "standard"
    assert "Standard" in window.ocr_status.text()
    window.ocr_tier.setCurrentIndex(window.ocr_tier.findData("advanced"))
    changed = json.loads(window.ocr_command.toPlainText())
    assert mineru_command_tier(changed) == "advanced"
    assert changed[:-2] == command[:-5] + ["--pages", "all", "--ocr-mode", "auto"]
    assert "Advanced" in window.ocr_status.text()
    assert window.save_settings(quiet=True)
    assert window.settings.data["ocr"]["command"] == changed
    assert window.settings.data["grading"] == before_grading
    restarted = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(restarted)
    assert restarted.ocr_tier.currentData() == "advanced"
    assert json.loads(restarted.ocr_command.toPlainText()) == changed


@pytest.mark.parametrize("tier_args, expected", [
    (["--tier", "basic"], "basic"), (["--tier=flash"], "flash"),
    (["--tier=future"], ""), ([], ""),
])
def test_manual_command_edits_sync_selector_without_changing_text(window, tmp_path, tier_args, expected):
    text = json.dumps(local_command(tmp_path, *tier_args), indent=2)
    window.ocr_command.setPlainText(text)
    assert window.ocr_tier.currentData() == expected
    assert window.ocr_tier.isEnabled()
    assert window.ocr_command.toPlainText() == text


def test_custom_program_keeps_its_tier_argument_and_disables_local_choices(window):
    command = [sys.executable, "custom_ocr.py", "{input}", "{output}", "--tier", "standard"]
    window.ocr_command.setPlainText(json.dumps(command))
    assert not window.ocr_tier.isEnabled()
    assert not window.ocr_text_source.isEnabled()
    assert not window.pdf_text_aid.isEnabled()
    assert window.ocr_tier.currentData() == ""
    assert "自定义" in window.ocr_status.text()
    assert window.save_settings(quiet=True)
    assert window.settings.data["ocr"]["command"] == command


@pytest.mark.parametrize("mode", ["http", "mineru_v1"])
def test_api_modes_do_not_apply_local_tier(window, tmp_path, mode):
    command = local_command(tmp_path, "--tier", "standard", "--ocr-mode=auto")
    text = json.dumps(command)
    window.ocr_command.setPlainText(text)
    window.ocr_mode.setCurrentText(mode)
    assert not window.ocr_tier.isEnabled()
    assert not window.ocr_text_source.isEnabled()
    assert not window.pdf_text_aid.isEnabled()
    assert "本地档位选择不生效" in window.ocr_status.text()
    assert window.ocr_endpoint.isEnabled()
    window.ocr_tier.setCurrentIndex(window.ocr_tier.findData("advanced"))
    window.ocr_text_source.setCurrentIndex(window.ocr_text_source.findData("ocr"))
    assert window.ocr_command.toPlainText() == text
    window.ocr_mode.setCurrentText("command")
    assert window.ocr_tier.currentData() == "standard"
    assert window.ocr_text_source.currentData() == "auto"


def test_invalid_command_disables_tier_selector_without_replacing_input(window):
    window.ocr_command.setPlainText("[")
    assert not window.ocr_tier.isEnabled()
    assert not window.ocr_text_source.isEnabled()
    assert window.ocr_command.toPlainText() == "["
    assert "格式无效" in window.ocr_status.text()


def test_forced_visual_ocr_is_saved_independently_of_tier_and_rerun_checkbox(window, tmp_path, qtbot):
    command = local_command(tmp_path, "--tier=advanced", "--ocr-mode=auto", "--pages", "all")
    window.ocr_command.setPlainText(json.dumps(command))
    assert window.ocr_text_source.currentData() == "auto"
    assert not window.force_ocr.isChecked()
    window.ocr_text_source.setCurrentIndex(window.ocr_text_source.findData("ocr"))
    changed = json.loads(window.ocr_command.toPlainText())
    assert mineru_command_ocr_mode(changed) == "ocr"
    assert mineru_command_tier(changed) == "advanced"
    assert not window.force_ocr.isChecked()
    assert "强制视觉重识别" in window.ocr_status.text()
    assert window.save_settings(quiet=True)
    restarted = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(restarted)
    assert restarted.ocr_text_source.currentData() == "ocr"
    assert restarted.ocr_tier.currentData() == "advanced"
    assert restarted.settings.data["ocr"]["command"] == changed
    assert not restarted.force_ocr.isChecked()


@pytest.mark.parametrize("args, expected", [(["--ocr-mode", "txt"], "txt"),
                                          (["--ocr-mode=ocr"], "ocr"),
                                          (["--ocr-mode=future"], ""), ([], "")])
def test_text_source_manual_edits_sync_without_rewriting_command(window, tmp_path, args, expected):
    text = json.dumps(local_command(tmp_path, "--tier=advanced", *args), indent=2)
    window.ocr_command.setPlainText(text)
    assert window.ocr_text_source.currentData() == expected
    assert window.ocr_command.toPlainText() == text


def test_optional_pdf_text_aid_saves_checkbox_and_does_not_live_in_advanced_json(window, tmp_path, qtbot):
    command = local_command(tmp_path, "--tier=advanced", "--ocr-mode=ocr")
    window.ocr_command.setPlainText(json.dumps(command))
    assert window.pdf_text_aid.isEnabled()
    assert not window.pdf_text_aid.isChecked()
    assert "pdf_text_aid" not in json.loads(window.advanced.toPlainText())["ocr"]
    window.pdf_text_aid.setChecked(True)
    assert "PDF 补识别：已开启" in window.ocr_status.text()
    assert "300 DPI" in window.pdf_text_aid.toolTip()
    assert "不用于纠正答案" in window.pdf_text_aid.toolTip()
    extra = json.loads(window.advanced.toPlainText())
    extra["ocr"]["pdf_text_aid"] = False  # Explicit checkbox wins over a manually added duplicate.
    window.advanced.setPlainText(json.dumps(extra))
    assert window.save_settings(quiet=True)
    assert window.settings.data["ocr"]["pdf_text_aid"] is True
    assert window.settings.data["ocr"]["command"] == command
    restarted = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(restarted)
    assert restarted.pdf_text_aid.isChecked()
    assert "pdf_text_aid" not in json.loads(restarted.advanced.toPlainText())["ocr"]


@pytest.mark.parametrize("mode, command", [
    ("command", ["mineru-kit", "parse", "{input}", "-o", "{output}", "--tier=advanced"]),
    ("command", [sys.executable, "custom.py", "{input}", "{output}"]),
    ("http", None), ("mineru_v1", None),
])
def test_pdf_text_aid_only_available_for_app_wrapper_and_preserves_preference(window, tmp_path, mode, command):
    wrapper = local_command(tmp_path, "--tier=advanced", "--ocr-mode=ocr")
    window.ocr_command.setPlainText(json.dumps(wrapper))
    window.pdf_text_aid.setChecked(True)
    if command is not None:
        window.ocr_command.setPlainText(json.dumps(command))
    window.ocr_mode.setCurrentText(mode)
    assert not window.pdf_text_aid.isEnabled()
    assert window.pdf_text_aid.isChecked()
    assert "补识别" in window.ocr_status.text()
    window.ocr_command.setPlainText(json.dumps(wrapper))
    window.ocr_mode.setCurrentText("command")
    assert window.pdf_text_aid.isEnabled()
    assert window.pdf_text_aid.isChecked()
