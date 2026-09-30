import copy
import json
import threading

import pytest

from bb_assistant.app import MainWindow, actor_critic_details
from bb_assistant.settings import Settings


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    for variable in ("BBMARK_GPT_API_KEY", "BBMARK_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *args: "")
    monkeypatch.setattr("keyring.delete_password", lambda *args: None)
    widget = MainWindow(tmp_path / "data", demo=True)
    qtbot.addWidget(widget)
    return widget


def add_collaboration(window, status="accepted"):
    row = window.store.get_attempt("demo-attempt-0")
    provenance = copy.deepcopy(row["provenance"])
    rubric = copy.deepcopy(window.current_rubric())
    trace = {
        "status": status, "revisions": 1, "max_revisions": 1,
        "events": [
            {"role": "actor", "round": 0, "result": {"score": 8, "rationale": "初评未注意被划去的答案"},
             "metadata": {"question_assessments": [{"question_id": "3", "verdict": "wrong", "reason": "所读选项不符"}]}},
            {"role": "critic", "round": 0, "result": {
                "decision": "revise", "suggested_score": 10,
                "question_assessments": [{"question_id": "3", "verdict": "correct", "reason": "最终填写 B"}],
                "issues": [{"question_id": "3", "kind": "reading", "evidence": "原图第 1 页，第 3 小问存在划改",
                            "feedback": "采用未划去的 B"}], "uncertainties": []}},
            {"role": "actor", "round": 1, "result": {"score": 10, "rationale": "原图最终答案正确"}},
            {"role": "critic", "round": 1, "result": {"decision": "accept", "summary": "修订依据一致"}},
        ],
        "final_decision": "已检查修订后的逐题依据",
    }
    snapshot = {"score": 10, "comment": "", "rationale": "原图最终答案正确", "rubric": rubric,
                "uncertainties": [], "actor_critic": trace}
    provenance.update(active_grader="actor_critic", actor_critic=trace, grades={
        "gpt": {"score": 7, "rationale": "旧的 GPT 独立结果", "rubric": rubric},
        "deepseek": {"score": 9, "rationale": "旧的 DS 独立结果", "rubric": rubric},
        "actor_critic": snapshot,
    })
    window.store.update_attempt(row["id"], provenance=provenance, ai_score=10, ai_comment="", status="graded")
    window.refresh_attempts()
    return provenance


def test_collaboration_button_runs_new_provider_with_current_prompt(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "save_settings", lambda **kwargs: True)
    monkeypatch.setattr(window, "start", lambda fn: fn())
    monkeypatch.setattr(window.workflow, "process", lambda *args, **kwargs: calls.append(
        (args, kwargs, copy.deepcopy(window.settings.data["rubric"]))))
    window.rubric_text.setPlainText("按当前参考答案复核每个小问")
    window.actor_critic_button.click()
    assert calls[0][0] == ("demo-homework", "grade")
    assert calls[0][1] == {"force_ocr": False, "provider": "actor_critic"}
    assert calls[0][2]["instructions"] == "按当前参考答案复核每个小问"
    assert {"GPT 评分", "DS 评分"}.issubset({button.text() for button in window.mutation_buttons})


def test_collaboration_trace_and_independent_results_are_distinct(window):
    add_collaboration(window)
    assert window.attempt_table.item(0, 2).text() == "7"
    assert window.attempt_table.item(0, 3).text() == "9"
    assert window.attempt_table.item(0, 4).text() == "DS→GPT: 10"
    assert "旧的 GPT 独立结果" in window.gpt_rationale.toPlainText()
    assert "旧的 DS 独立结果" in window.ds_rationale.toPlainText()
    text = window.collaboration_rationale.toPlainText()
    assert "—— DS 初评 ——" in text
    assert "DS 第 1 次修订" in text
    assert "GPT 第 2 次复核" in text
    assert "DS 建议分数：8" in text
    assert "GPT 核算分数：10" in text
    assert "复核结论：要求修订" in text
    assert "第 3 小问：错误。所读选项不符" in text
    assert "第 3 小问：正确。最终填写 B" in text
    assert "类型：作答识读" in text
    assert "原文依据：原图第 1 页，第 3 小问存在划改" in text
    assert "修订建议：采用未划去的 B" in text
    assert '"question_assessments"' not in text
    assert '"decision"' not in text
    assert window.collaboration_rationale.isReadOnly()
    assert window.grade_source.currentData() == "actor_critic"
    assert window.collaboration_status.text() == "模型复核通过，仍需人工审核"
    assert window.store.get_attempt("demo-attempt-0")["reviewed_score"] is None
    assert "DS→GPT：与当前规则一致" in window.rule_status.text()


def test_unresolved_disagreement_visible_and_manually_selectable(window, monkeypatch):
    add_collaboration(window, "needs_human")
    assert "待人工裁定" in window.collaboration_status.text()
    assert "#b42318" in window.collaboration_status.styleSheet()
    assert "协作待裁定" in window.attempt_table.item(0, 7).text()
    calls = []
    monkeypatch.setattr(window.workflow, "select_grade", lambda *args: calls.append(args))
    window.use_grade_button.click()
    assert calls == [("demo-attempt-0", "actor_critic")]
    assert "DS→GPT 建议" in window.log_box.toPlainText()


def test_incomplete_latest_trace_overrides_old_snapshot_and_blocks_adoption(window):
    provenance = add_collaboration(window)
    provenance["actor_critic"] = {
        "status": "incomplete", "revisions": 0, "max_revisions": 1,
        "events": [{"role": "actor", "round": 0, "result": {"score": 9}}],
        "error": "GPT 复核服务超时",
    }
    window.store.update_attempt("demo-attempt-0", provenance=provenance, status="error")
    window.refresh_attempts()
    assert "协作未完成" in window.collaboration_status.text()
    assert "GPT 复核服务超时" in window.collaboration_rationale.toPlainText()
    assert "DS 第 1 次修订" not in window.collaboration_rationale.toPlainText()
    assert "原图最终答案正确" not in window.collaboration_rationale.toPlainText()
    assert not window.use_grade_button.isEnabled()
    window.grade_source.setCurrentIndex(window.grade_source.findData("gpt"))
    assert window.use_grade_button.isEnabled()


@pytest.mark.parametrize("status", ["reviewed", "uploaded", "bb_graded", "upload_uncertain"])
def test_protected_records_cannot_adopt_collaboration(window, status):
    add_collaboration(window)
    if status == "bb_graded":
        window.store.update_attempt("demo-attempt-0", status=status)
    else:
        window.store.approve("demo-attempt-0", 10, "", 10)
        if status != "reviewed":
            window.store.begin_upload("demo-attempt-0")
            if status == "uploaded":
                window.store.mark_uploaded("demo-attempt-0", {"verified": True})
            else:
                window.store.mark_upload_uncertain("demo-attempt-0", "单元测试上传待核验")
    window.refresh_attempts()
    assert not window.grade_source.isEnabled()
    assert not window.use_grade_button.isEnabled()


def test_collaboration_controls_disabled_while_processing(window, qtbot):
    add_collaboration(window)
    release = threading.Event()
    try:
        window.start(lambda: release.wait(5))
        assert not window.actor_critic_button.isEnabled()
        assert not window.grade_source.isEnabled()
        assert not window.use_grade_button.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.busy, timeout=10000)
    assert window.actor_critic_button.isEnabled()
    assert window.use_grade_button.isEnabled()


def test_collaboration_rule_warning_updates_without_resetting_human_edits(window):
    add_collaboration(window)
    window.score.setValue(9.25)
    window.comment.setPlainText("人工暂存")
    window.reference.setPlainText("新参考答案")
    assert "DS→GPT：与当前规则不同" in window.rule_status.text()
    assert "与当前规则不同" in window.collaboration_rationale.toPlainText()
    assert window.score.value() == 9.25
    assert window.comment.toPlainText() == "人工暂存"


def test_collaboration_settings_roundtrip_preserves_advanced_keys(window, qtbot):
    original_gpt = copy.deepcopy(window.settings.data["gpt"])
    original_ds = copy.deepcopy(window.settings.data["grading"])
    advanced = json.loads(window.advanced.toPlainText())
    advanced["actor_critic"]["future_option"] = "preserved"
    window.advanced.setPlainText(json.dumps(advanced))
    window.actor_critic_revisions.setValue(2)
    assert window.save_settings(quiet=True)
    assert window.settings.data["actor_critic"] == {"max_revisions": 2, "future_option": "preserved"}
    assert window.settings.data["gpt"] == original_gpt
    assert window.settings.data["grading"] == original_ds
    restarted = MainWindow(window.settings.root, demo=True)
    qtbot.addWidget(restarted)
    assert restarted.actor_critic_revisions.value() == 2


@pytest.mark.parametrize("value", [True, False, -1, 3, 1.0, "1", None])
def test_collaboration_settings_reject_invalid_revision_limit(tmp_path, value):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data["actor_critic"]["max_revisions"] = value
    with pytest.raises(ValueError, match="修订次数"):
        settings.save(data)
    assert not settings.path.exists()


@pytest.mark.parametrize("value", [0, 1, 2])
def test_collaboration_settings_accept_revision_boundaries(tmp_path, value):
    settings = Settings(tmp_path)
    data = copy.deepcopy(settings.data)
    data["actor_critic"]["max_revisions"] = value
    settings.save(data)
    assert Settings(tmp_path).data["actor_critic"]["max_revisions"] == value


def test_legacy_settings_default_collaboration_limit_and_empty_trace(tmp_path):
    (tmp_path / "settings.json").write_text('{"grading":{"model":"existing-model"}}', encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.data["actor_critic"]["max_revisions"] == 1
    assert settings.data["grading"]["model"] == "existing-model"
    assert "尚无协作复核记录" in actor_critic_details({})
