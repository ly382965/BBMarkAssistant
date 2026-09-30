import copy
import json

import pytest

from bb_assistant.app import MainWindow, grading_details, recognition_label


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    for name in ("BBMARK_GPT_API_KEY", "BBMARK_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *args: "")
    monkeypatch.setattr("keyring.delete_password", lambda *args: None)
    window = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(window)
    return window


def test_buttons_regrade_selected_provider_and_save_prompt_each_click(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "save_settings", lambda **kwargs: True)
    monkeypatch.setattr(window, "start", lambda fn: fn())
    monkeypatch.setattr(window.workflow, "process", lambda *args, **kwargs: calls.append(
        (args, kwargs, copy.deepcopy(window.settings.data["rubric"]))))
    for index, (label, provider) in enumerate([("GPT 评分", "gpt"), ("DS 评分", "deepseek"), ("GPT 评分", "gpt")]):
        window.rubric_text.setPlainText(f"规则 {index}")
        next(button for button in window.mutation_buttons if button.text() == label).click()
        assert calls[-1][0] == ("demo-homework", "grade")
        assert calls[-1][1] == {"force_ocr": False, "provider": provider}
        assert calls[-1][2]["instructions"] == f"规则 {index}"
    next(button for button in window.mutation_buttons if button.text() == "一键处理（DS）").click()
    assert calls[-1][0] == ("demo-homework", "all")
    assert calls[-1][1]["provider"] == "deepseek"


def add_provider_results(window):
    row = window.store.get_attempt("demo-attempt-0")
    provenance = copy.deepcopy(row["provenance"])
    provenance.update(active_grader="deepseek", grades={
        "gpt": {"score": 8, "comment": "GPT 评语", "rationale": "GPT 原图依据", "uncertainties": ["图像疑点"], "rubric": provenance["rubric"]},
        "deepseek": {"score": 9, "comment": "DS 评语", "rationale": "DS 文字依据", "uncertainties": [], "rubric": provenance["rubric"]},
    }, recognition={"documents": [{"name": "a.pdf", "route": "vision"}, {"name": "b.txt", "route": "native"}]})
    for provider, grade in provenance["grades"].items():
        grade.update(grading={"model": provider + "-fixture"}, input_sha256="synthetic-input")
        grade["rubric"] = copy.deepcopy(window.current_rubric())
    window.store.update_attempt(row["id"], provenance=provenance, status="graded")
    window.refresh_attempts()


def test_separate_scores_evidence_and_routes_visible_without_averaging(window):
    add_provider_results(window)
    assert window.attempt_table.item(0, 2).text() == "8"
    assert window.attempt_table.item(0, 3).text() == "9"
    assert window.attempt_table.item(0, 4).text() == "DS: 9"
    assert "直接提取 + 原图" in window.detail_meta.text()
    assert "GPT 原图依据" in window.gpt_rationale.toPlainText()
    assert "DS 文字依据" in window.ds_rationale.toPlainText()
    assert "图像疑点" in window.gpt_rationale.toPlainText()
    assert window.score.value() == 9


def test_history_score_never_mislabeled_as_gpt_or_ds(window):
    assert window.attempt_table.item(0, 2).text() == ""
    assert window.attempt_table.item(0, 3).text() == ""
    assert window.attempt_table.item(0, 4).text() == "历史 AI: 9"
    assert not window.use_grade_button.isEnabled()


def test_adopting_grade_calls_explicit_provider_and_reviewed_record_is_protected(window, monkeypatch):
    add_provider_results(window)
    calls = []
    monkeypatch.setattr(window.workflow, "select_grade", lambda *args: calls.append(args), raising=False)
    window.grade_source.setCurrentIndex(window.grade_source.findData("gpt"))
    window.use_grade_button.click()
    assert calls == [("demo-attempt-0", "gpt")]
    window.store.approve("demo-attempt-0", 9, "", 10)
    window.refresh_attempts()
    assert not window.use_grade_button.isEnabled()
    assert not window.grade_source.isEnabled()
    window.use_grade()
    assert len(calls) == 1


def test_adoption_updates_review_fields_but_retains_both_suggestions(window):
    add_provider_results(window)
    window.grade_source.setCurrentIndex(window.grade_source.findData("gpt"))
    window.use_grade_button.click()
    row = window.store.get_attempt("demo-attempt-0")
    assert row["ai_score"] == window.score.value() == 8
    assert row["ai_comment"] == window.comment.toPlainText() == "GPT 评语"
    assert row["provenance"]["active_grader"] == "gpt"
    assert row["provenance"]["grades"]["deepseek"]["score"] == 9
    assert row["reviewed_score"] is None
    assert "GPT 原图依据" in window.rationale.toPlainText()


def test_each_provider_shows_rubric_mismatch_without_resetting_manual_edits(window):
    add_provider_results(window)
    row = window.store.get_attempt("demo-attempt-0")
    provenance = copy.deepcopy(row["provenance"])
    provenance["grades"]["deepseek"]["rubric"]["instructions"] = "此前的规则"
    window.store.update_attempt(row["id"], provenance=provenance, status="graded")
    window.refresh_attempts()
    assert "GPT：与当前规则一致" in window.rule_status.text()
    assert "DS：与当前规则不同" in window.rule_status.text()
    assert "与当前规则不同" in window.ds_rationale.toPlainText()
    window.score.setValue(8.25)
    window.comment.setPlainText("正在编辑的人工评语")
    window.reference.setPlainText("修订参考答案")
    assert "GPT：与当前规则不同" in window.rule_status.text()
    assert window.score.value() == 8.25
    assert window.comment.toPlainText() == "正在编辑的人工评语"
    assert window.store.get_attempt(row["id"])["provenance"] == provenance


def test_rule_policy_changes_and_unknown_historical_rules_are_visible(window):
    assert "历史规则快照不完整" in window.rule_status.text()
    add_provider_results(window)
    window.count_errors.setChecked(True)
    assert "GPT：与当前规则不同" in window.rule_status.text()
    assert "DS：与当前规则不同" in window.rule_status.text()


def test_vision_route_visible_when_cached_text_only_contains_attachment_headers(window):
    add_provider_results(window)
    row = window.store.get_attempt("demo-attempt-0")
    window.store.update_attempt(row["id"], ocr_text="--- 附件 source.pdf，第 1 页 ---",
                                provenance=row["provenance"], ai_score=9, status="graded")
    window.refresh_attempts()
    assert "原图路线" in window.ocr_text.toPlainText()
    assert "直接读取附件图像" in window.ocr_text.toPlainText()
    assert "source.pdf" in window.ocr_text.toPlainText()


def test_legacy_forced_ocr_and_model_snapshot_route_labels():
    assert recognition_label({"provenance": {"ocr": [{"format": "pdf"}]}}) == "OCR"
    snapshot = {"score": 9, "rationale": "依据", "recognition": {"documents": [{"route": "vision"}]}}
    assert "本模型输入路线：原图" in grading_details(snapshot)


def test_provider_adoption_controls_disabled_while_worker_runs(window, qtbot):
    import threading
    add_provider_results(window)
    release = threading.Event()
    try:
        window.start(lambda: release.wait(5))
        assert not window.grade_source.isEnabled()
        assert not window.use_grade_button.isEnabled()
        window.attempt_table.selectRow(1)
        window.attempt_table.selectRow(0)
        assert not window.use_grade_button.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.busy, timeout=10000)
    assert window.grade_source.isEnabled()
    assert window.use_grade_button.isEnabled()


def test_gpt_settings_and_recognition_saved_independently(window, qtbot):
    original = copy.deepcopy(window.settings.data["grading"])
    window.gpt_url.setText("https://proxy.example/v1")
    window.gpt_model.setText("vision-model")
    window.recognition_mode.setCurrentIndex(window.recognition_mode.findData("vision"))
    advanced = json.loads(window.advanced.toPlainText())
    advanced["gpt"]["http_headers"] = {"x-openai-actor-authorization": "local-image-extension"}
    window.advanced.setPlainText(json.dumps(advanced))
    assert window.save_settings(quiet=True)
    assert window.settings.data["grading"] == original
    restarted = MainWindow(window.settings.root, demo=True)
    qtbot.addWidget(restarted)
    assert restarted.gpt_url.text() == "https://proxy.example/v1"
    assert restarted.gpt_model.text() == "vision-model"
    assert restarted.gpt_wire.currentText() == "responses"
    assert restarted.recognition_mode.currentData() == "vision"


def test_environment_keys_redacted_but_not_copied_to_fields_or_vault(window, monkeypatch, qtbot):
    monkeypatch.setenv("BBMARK_GPT_API_KEY", "gpt-environment-fixture")
    monkeypatch.setenv("BBMARK_DEEPSEEK_API_KEY", "ds-environment-fixture")
    restarted = MainWindow(window.settings.root, demo=True)
    qtbot.addWidget(restarted)
    assert restarted.gpt_key.text() == ""
    assert restarted.ds_key.text() == ""
    assert restarted.redact("gpt-environment-fixture ds-environment-fixture") == "[凭据已隐藏] [凭据已隐藏]"
    writes = []
    monkeypatch.setattr("keyring.set_password", lambda *args: writes.append(args))
    restarted.remember_keys.setChecked(True)
    assert restarted.save_settings(quiet=True)
    assert not writes
