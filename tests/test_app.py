from bb_assistant.app import MainWindow
import pytest


def test_local_mineru_button_recovers_api_mode_without_changing_grading(qtbot, tmp_path, monkeypatch):
    import copy
    import json
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    before = copy.deepcopy(window.settings.data["grading"])
    local = ["C:/local/python.exe", "C:/local/mineru_local.py", "parse", "{input}", "-o", "{output}/document.md"]
    monkeypatch.setattr("bb_assistant.app.discover_local_command", lambda: local)
    monkeypatch.setattr("bb_assistant.mineru_runtime.discover_local_command", lambda *args, **kwargs: local)
    window.ocr_mode.setCurrentText("http")
    assert window.ocr_endpoint.isEnabled()
    window.use_local_mineru()
    assert window.ocr_mode.currentText() == "command"
    assert json.loads(window.ocr_command.toPlainText()) == local
    assert not window.ocr_endpoint.isEnabled()
    assert window.settings.data["ocr"]["command"] == local
    assert window.settings.data["grading"] == before


def test_demo_roster_scope_and_human_review(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.show()
    assert window.student_table.rowCount() == 6
    assert window.attempt_table.rowCount() == 4
    window.scope_only.setChecked(True)
    assert window.student_table.rowCount() == 4
    window.student_search.setText("PB25000262")
    assert window.student_table.rowCount() == 1
    window.score.setValue(8.25)
    window.comment.setPlainText("人工修改：补充复杂度分析。")
    window.approve_button.click()
    row = window.store.get_attempt("demo-attempt-0")
    assert row["reviewed_score"] == 8.25
    assert row["status"] == "reviewed"
    assert window.revoke_button.isEnabled()
    window.revoke_button.click()
    assert window.store.get_attempt("demo-attempt-0")["status"] == "graded"


@pytest.mark.parametrize("comment", ["", "  \n\t "])
def test_final_comment_can_be_cleared_and_stays_blank_after_reload(qtbot, tmp_path, monkeypatch, comment):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    warnings = []
    monkeypatch.setattr("bb_assistant.app.QMessageBox.warning", lambda *args: warnings.append(args[-1]))
    assert window.comment.toPlainText()
    window.comment.setPlainText(comment)
    window.approve_button.click()
    row = window.store.get_attempt("demo-attempt-0")
    assert row["status"] == "reviewed"
    assert row["reviewed_comment"] == ""
    assert row["ai_comment"]  # The suggestion must not replace an intentionally blank review.
    assert not warnings
    window.attempt_table.selectRow(1)
    window.attempt_table.selectRow(0)
    assert window.comment.toPlainText() == ""
    second = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(second)
    assert second.comment.toPlainText() == ""


def test_ocr_defaults_to_missing_and_force_is_only_for_one_explicit_run(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    calls = []
    monkeypatch.setattr(window, "save_rubric", lambda **kwargs: True)
    monkeypatch.setattr(window, "save_settings", lambda **kwargs: True)
    monkeypatch.setattr(window, "start", lambda fn: fn())
    monkeypatch.setattr(window.workflow, "process", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert not window.force_ocr.isChecked()
    window.process("ocr")
    assert calls[-1] == (("demo-homework", "ocr"), {"force_ocr": False})
    window.force_ocr.setChecked(True)
    window.process("ocr")
    assert calls[-1][1] == {"force_ocr": True}
    assert not window.force_ocr.isChecked()
    window.process("ocr")
    assert calls[-1][1] == {"force_ocr": False}
    window.force_ocr.setChecked(True)
    window.process("all")
    assert calls[-1] == (("demo-homework", "all"), {"force_ocr": False})


def test_regrade_button_saves_current_prompt_on_every_click(qtbot, tmp_path, monkeypatch):
    import copy
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    calls = []
    monkeypatch.setattr(window, "save_settings", lambda **kwargs: True)
    monkeypatch.setattr(window, "start", lambda fn: fn())
    monkeypatch.setattr(window.workflow, "process", lambda *args, **kwargs: calls.append(
        (args, copy.deepcopy(window.settings.data["rubric"]))))
    button = next(b for b in window.mutation_buttons if b.text() == "③ 重新评分")
    for index in (1, 2):
        window.rubric_text.setPlainText(f"修订规则 {index}")
        window.reference.setPlainText(f"修订参考答案 {index}")
        window.max_score.setValue(index * 10)
        button.click()
        assert calls[-1] == (("demo-homework", "grade"), {
            "instructions": f"修订规则 {index}", "reference_answer": f"修订参考答案 {index}",
            "max_score": index * 10,
        })
    assert len(calls) == 2
    window.save_rubric()
    assert "③ 重新评分" in window.log_box.toPlainText()


def test_workbench_only_shows_latest_attempt_without_deleting_history(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.store.upsert_attempt("_200_1", "PB23000018", "demo-homework", submitted_at="26-9-28")
    window.refresh_attempts()
    assert window.attempt_table.rowCount() == 4
    assert {row["id"] for row in window.rows} == {"_200_1", "demo-attempt-1", "demo-attempt-2", "demo-attempt-3"}
    assert len(window.store.list_attempts("demo-homework")) == 5
    assert "已忽略 1 份旧尝试" in window.attempt_policy.text()
    assert window.store.get_attempt("demo-attempt-0")["ai_score"] == 9


def test_ambiguous_attempt_order_keeps_window_available_for_resync(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.store.upsert_attempt("opaque-new", "PB23000018", "demo-homework", submitted_at="")
    window.refresh_attempts()
    assert window.attempt_table.rowCount() == 0
    assert "无法确定最新提交" in window.attempt_policy.text()
    assert not window.approve_button.isEnabled()


def test_worker_completes_and_reenables_controls(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.start(lambda: window.workflow.log("后台任务验证"))
    qtbot.waitUntil(lambda: not window.busy, timeout=10000)
    assert "后台任务验证" in window.log_box.toPlainText()
    assert window.assignment_combo.isEnabled()
    assert window.force_ocr.isEnabled()


def test_pages_and_rubric_survive_restart(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.rubric_text.setPlainText("每题 5 分，按推理步骤给分。")
    window.reference.setPlainText("第一题 O(n)，第二题 O(log n)。")
    assert window.save_rubric(quiet=True)
    for index in range(4):
        window.nav.setCurrentRow(index)
        assert window.pages.currentIndex() == index
    second = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(second)
    assert second.max_score.value() == 10
    assert second.rubric_text.toPlainText() == "每题 5 分，按推理步骤给分。"


def test_error_count_policy_is_saved_per_assignment_and_can_be_disabled(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    assert not window.count_errors.isChecked()
    assert not window.free_errors.isEnabled()
    window.count_errors.setChecked(True)
    window.free_errors.setValue(1)
    window.error_deduction.setValue(0.5)
    window.error_unit.setCurrentIndex(window.error_unit.findData("major_question"))
    assert window.save_rubric(quiet=True)
    policy = {"mode": "error_count", "free_errors": 1, "deduction_per_error": 0.5, "unit": "major_question"}
    assert window.settings.data["rubric"]["scoring_policy"] == policy
    second = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(second)
    assert second.count_errors.isChecked()
    assert second.free_errors.value() == 1
    assert second.error_deduction.value() == 0.5
    window.store.upsert_assignment("second", "第二份作业", 10)
    window.refresh()
    window.assignment_combo.setCurrentIndex(window.assignment_combo.findData("second"))
    assert not window.count_errors.isChecked()
    window.assignment_combo.setCurrentIndex(window.assignment_combo.findData("demo-homework"))
    assert window.count_errors.isChecked()
    window.count_errors.setChecked(False)
    assert window.save_rubric(quiet=True)
    assert "scoring_policy" not in window.settings.data["rubric"]


def test_rubrics_are_separate_between_assignments(qtbot, tmp_path):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.rubric_text.setPlainText("第一份作业评分标准")
    window.store.upsert_assignment("second", "第二份作业", 10)
    window.refresh()
    window.assignment_combo.setCurrentIndex(window.assignment_combo.findData("second"))
    assert window.rubric_text.toPlainText() == ""
    window.rubric_text.setPlainText("第二份规则")
    window.assignment_combo.setCurrentIndex(window.assignment_combo.findData("demo-homework"))
    assert window.rubric_text.toPlainText() == "第一份作业评分标准"


def test_approval_rejects_changed_scale_and_popup_redacts(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    warnings = []
    monkeypatch.setattr("bb_assistant.app.QMessageBox.warning", lambda parent, title, message: warnings.append(message))
    window.max_score.setValue(100)
    window.approve()
    assert window.store.get_attempt("demo-attempt-0")["status"] == "graded"
    assert "满分" in warnings[-1]
    window.ds_key.setText("fake-secret")
    window.fail("provider says fake-secret")
    assert "fake-secret" not in warnings[-1]
