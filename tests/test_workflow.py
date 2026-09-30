"""Mocked download → OCR → grade → human review → BB write integration."""

import copy
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from bb_assistant.blackboard import Assignment, Attempt, GradeUploadBlockedError, Student
from bb_assistant.settings import DEFAULTS
from bb_assistant.storage import Store
from bb_assistant.workflow import Workflow, expand_documents, safe_id, seed_demo


@pytest.fixture
def setup(tmp_path):
    data = copy.deepcopy(DEFAULTS)
    # This suite exercises the explicit OCR/text workflow. Automatic routing is
    # covered separately with real rendered images and a classifier stub.
    data["recognition"] = {"mode": "ocr"}
    data["rubric"].update(instructions="每题按参考答案给分", reference_answer="参考答案", max_score=10)
    settings = SimpleNamespace(root=tmp_path, data=data, secret=lambda name: "")
    store = Store(tmp_path)
    workflow = Workflow(settings, store)
    client = MagicMock()
    client.list_students.return_value = [Student("PB25000001", "范围内学生", "u1"), Student("PB25000263", "范围外学生", "u2")]
    client.list_assignments.return_value = [Assignment("hw", "作业", 10)]
    client.list_attempts.return_value = [
        Attempt("a1", "PB25000001", "hw", "https://www.bb.ustc.edu.cn/a1", "2026-09-27", "NeedsGrading"),
        Attempt("a2", "PB25000263", "hw", "https://www.bb.ustc.edu.cn/a2", "2026-09-27", "NeedsGrading"),
    ]

    def download(attempt, destination):
        destination.mkdir(parents=True, exist_ok=True)
        pdf = destination / "解答.pdf"
        pdf.write_bytes(b"fake PDF; OCR is mocked")
        return [pdf]

    client.download_attempt.side_effect = download
    client.upload_grade.return_value = {"verified": True, "score": 9, "attempt_id": "a1"}
    workflow.client = client
    workflow.sync()
    workflow.sync_attempts("hw")
    return SimpleNamespace(settings=settings, store=store, workflow=workflow, client=client, root=tmp_path)


@pytest.fixture
def services():
    with patch("bb_assistant.workflow.OcrClient") as ocr_type, patch("bb_assistant.workflow.GradingClient") as grade_type:
        ocr = ocr_type.return_value
        ocr.extract.return_value = "学生作业识别文本"
        ocr.last_metadata = {"backend": "fake-mineru", "input_sha256": "pdfhash"}
        grader = grade_type.return_value
        grader.grade.return_value = {"score": 8, "comment": "基本正确", "rationale": "第 2 题缺少条件", "uncertainties": []}
        grader.last_metadata = {"model": "fake-ds", "prompt_hash": "systemhash"}
        yield SimpleNamespace(ocr_type=ocr_type, grade_type=grade_type, ocr=ocr, grader=grader)


def test_full_workflow_uploads_human_values_and_exports_report(setup, services):
    summary = setup.workflow.process("hw")
    assert summary["completed"] == 1
    assert summary["awaiting_review"] == 1
    assert summary["score_mean"] == 8
    assert Path(summary["report_path"]).is_file()
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert len(report["attempts"]) == 2
    row = setup.store.get_attempt("a1")
    assert row["status"] == "graded"
    assert row["ai_score"] == 8
    assert row["reviewed_score"] is None
    assert row["provenance"]["ocr"][0]["backend"] == "fake-mineru"
    assert row["provenance"]["grading"]["model"] == "fake-ds"
    assert row["provenance"]["rubric"]["max_score"] == 10
    assert setup.store.get_attempt("a2")["status"] == "submitted"
    setup.client.download_attempt.assert_called_once()
    setup.store.approve("a1", 9, "教师核对后的评语", 10)
    setup.workflow.upload(["a1"])
    args = setup.client.upload_grade.call_args.args
    assert args[0].id == "a1"
    assert args[1:] == (9, "教师核对后的评语")
    assert setup.store.get_attempt("a1")["status"] == "uploaded"


def test_missing_ocr_runtime_stops_batch_before_marking_student_failures(setup, services):
    services.ocr.preflight.side_effect = ValueError("本地命令未找到")
    with pytest.raises(ValueError, match="本地命令未找到"):
        setup.workflow.process("hw")
    assert setup.store.get_attempt("a1")["status"] == "submitted"
    setup.client.download_attempt.assert_not_called()
    services.ocr.extract.assert_not_called()
    services.grader.grade.assert_not_called()


def test_ocr_coverage_warning_survives_grading_and_regrading(setup, services):
    warning = "图片描述仅供核对，可能存在未识别答案。"
    services.ocr.last_metadata["warnings"] = [warning]
    setup.workflow.process("hw")
    assert setup.store.get_attempt("a1")["uncertainties"] == [warning]
    services.grader.grade.return_value["uncertainties"] = [warning, "模型自己的疑点"]
    setup.workflow.process("hw", "grade")
    assert setup.store.get_attempt("a1")["uncertainties"] == [warning, "模型自己的疑点"]
    assert services.ocr.extract.call_count == 1


def test_plain_text_batch_does_not_require_mineru(setup, services):
    path = setup.root / "answer.txt"
    path.write_text("纯文本作业", encoding="utf-8")
    setup.store.update_attempt("a1", paths=[str(path)], status="downloaded")
    services.ocr.preflight.side_effect = ValueError("本地命令未找到")
    result = setup.workflow.process("hw", "ocr")
    services.ocr.preflight.assert_not_called()
    assert result["completed"] == 1


def test_upload_preflight_rejects_unreviewed_without_network_write(setup):
    with pytest.raises(ValueError, match="人工审核"):
        setup.workflow.upload(["a1"])
    setup.client.upload_grade.assert_not_called()
    assert setup.store.get_attempt("a1")["status"] == "submitted"


@pytest.mark.parametrize("receipt", [{"verified": False}, {"verified": "true"}, None])
def test_bad_receipt_locks_attempt_and_stops_future_retries(setup, receipt):
    setup.store.approve("a1", 9, "已审核", 10)
    setup.client.upload_grade.return_value = receipt
    with pytest.raises(ValueError, match="上传结果待核验"):
        setup.workflow.upload(["a1"])
    assert setup.store.get_attempt("a1")["status"] == "upload_uncertain"
    with pytest.raises(ValueError, match="人工审核"):
        setup.workflow.upload(["a1"])
    assert setup.client.upload_grade.call_count == 1


def test_transport_failure_stops_batch_before_next_student(setup):
    setup.store.upsert_student("PB24000001", "第二名学生")
    setup.store.upsert_attempt("a3", "PB24000001", "hw")
    for attempt_id in ("a1", "a3"):
        setup.store.approve(attempt_id, 9, "已审核", 10)
    setup.client.upload_grade.side_effect = TimeoutError("request timed out")
    with pytest.raises(ValueError, match="上传结果待核验"):
        setup.workflow.upload(["a1", "a3"])
    assert setup.store.get_attempt("a1")["status"] == "upload_uncertain"
    assert setup.store.get_attempt("a3")["status"] == "reviewed"
    assert setup.client.upload_grade.call_count == 1


def test_presend_block_keeps_review_and_stops_before_next_student(setup):
    setup.store.upsert_student("PB24000001", "第二名学生")
    setup.store.upsert_attempt("a3", "PB24000001", "hw")
    for attempt_id in ("a1", "a3"):
        setup.store.approve(attempt_id, 9, "", 10)
    approved = setup.store.get_attempt("a1")
    setup.client.upload_grade.side_effect = GradeUploadBlockedError("不能识别提交地址")
    with pytest.raises(ValueError, match="未发送成绩") as caught:
        setup.workflow.upload(["a1", "a3"])
    assert "待核验" not in str(caught.value)
    blocked = setup.store.get_attempt("a1")
    assert blocked["status"] == "reviewed"
    assert blocked["reviewed_at"] == approved["reviewed_at"]
    assert blocked["reviewed_score"] == 9
    assert blocked["reviewed_comment"] == ""
    assert blocked["error"] == "不能识别提交地址"
    assert setup.store.list_audit("a1")[-1]["event"] == "upload_blocked_before_send"
    assert setup.store.get_attempt("a3")["status"] == "reviewed"
    assert setup.client.upload_grade.call_count == 1


def test_presend_error_is_redacted_before_persistence_and_display(setup):
    setup.settings.secret = lambda name: "upload-secret-token" if name == "blackboard" else ""
    setup.store.approve("a1", 9, "", 10)
    setup.client.upload_grade.side_effect = GradeUploadBlockedError("blocked upload-secret-token")
    with pytest.raises(ValueError, match="未发送成绩") as caught:
        setup.workflow.upload(["a1"])
    assert "upload-secret-token" not in str(caught.value)
    assert "upload-secret-token" not in setup.store.get_attempt("a1")["error"]
    assert caught.value.__suppress_context__


def test_current_rubric_max_or_server_max_change_blocks_upload(setup):
    setup.store.approve("a1", 9, "已审核", 10)
    setup.settings.data["rubric"]["max_score"] = 100
    with pytest.raises(ValueError, match="当前评分满分"):
        setup.workflow.upload(["a1"])
    setup.settings.data["rubric"]["max_score"] = 10
    setup.client.list_assignments.return_value = [Assignment("hw", "作业", 100)]
    with pytest.raises(ValueError, match="BB 作业满分"):
        setup.workflow.upload(["a1"])
    setup.client.upload_grade.assert_not_called()


def test_all_rows_are_preflighted_before_first_write(setup):
    setup.store.approve("a1", 9, "已审核", 10)
    with pytest.raises(ValueError):
        setup.workflow.upload(["a1", "a2"])
    setup.client.upload_grade.assert_not_called()
    assert setup.store.get_attempt("a1")["status"] == "reviewed"


def test_multiple_attempts_for_one_student_require_selection(setup):
    setup.store.upsert_attempt("a3", "PB25000001", "hw")
    for attempt_id in ("a1", "a3"):
        setup.store.approve(attempt_id, 9, "已审核", 10)
    with pytest.raises(ValueError, match="只能选择一份"):
        setup.workflow.upload(["a1", "a3"])
    setup.client.upload_grade.assert_not_called()


def test_sync_normalizes_remote_status_without_overwriting_progress(setup):
    setup.store.approve("a1", 9, "已审核", 10)
    setup.client.list_attempts.return_value = [
        Attempt("a1", "PB25000001", "hw", "url", status="NeedsGrading"),
        Attempt("done", "PB25000001", "hw", "url", status="Completed"),
        Attempt("reconcile", "PB25000001", "hw", "url", status="NeedsReconciliation"),
        Attempt("inprogress", "PB25000001", "hw", "url", status="InProgress"),
    ]
    setup.workflow.sync_attempts("hw")
    assert setup.store.get_attempt("a1")["status"] == "reviewed"
    assert setup.store.get_attempt("done")["status"] == "bb_graded"
    assert setup.store.get_attempt("reconcile")["status"] == "bb_reconciliation"
    with pytest.raises(KeyError):
        setup.store.get_attempt("inprogress")


def test_demo_seed_contains_ocr_and_ai_without_clearing_each_other(tmp_path):
    store = Store(tmp_path)
    seed_demo(store, tmp_path)
    assert len(store.list_students()) == 6
    assert len(store.list_attempts("demo-homework")) == 4
    row = store.get_attempt("demo-attempt-0")
    assert row["ocr_text"]
    assert row["ai_score"] == 9
    assert row["status"] == "graded"


def test_download_is_independent_of_ocr_and_grading_settings(setup, services):
    setup.workflow.process("hw", "download")
    services.ocr_type.assert_not_called()
    services.grade_type.assert_not_called()
    assert setup.store.get_attempt("a1")["status"] == "downloaded"


@pytest.mark.parametrize("stage", ["download", "ocr", "grade", "all"])
def test_every_processing_stage_ignores_older_attempts(setup, services, stage):
    setup.workflow.process("hw")
    old = setup.store.get_attempt("a1")
    answer = setup.root / "latest.txt"
    answer.write_text("最新提交", encoding="utf-8")
    setup.store.upsert_attempt("_200_1", "PB25000001", "hw", submitted_at="26-9-28")
    setup.store.update_attempt("_200_1", paths=[str(answer)],
                               ocr_text="最新识别正文" if stage == "grade" else "", status="downloaded")
    services.ocr.extract.reset_mock()
    services.grader.grade.reset_mock()
    setup.client.download_attempt.reset_mock()
    summary = setup.workflow.process("hw", stage)
    assert summary["completed"] == 1
    assert summary["ignored_old_attempts"] == 1
    assert summary["in_scope_attempts"] == 1
    assert setup.store.get_attempt("a1") == old
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert {row["id"] for row in report["attempts"]} == {"_200_1", "a2"}
    if stage == "download":
        assert setup.client.download_attempt.call_args.args[0].id == "_200_1"
    elif stage == "ocr":
        services.ocr.extract.assert_called_once()
        services.grader.grade.assert_not_called()
    else:
        services.grader.grade.assert_called_once()


@pytest.mark.parametrize("status", ["reviewed", "bb_graded"])
def test_latest_protected_attempt_does_not_fall_back_to_older_unreviewed(setup, services, status):
    setup.store.upsert_attempt("_200_1", "PB25000001", "hw", submitted_at="26-9-28")
    if status == "reviewed":
        setup.store.approve("_200_1", 10, "", 10)
    else:
        setup.store.update_attempt("_200_1", status=status)
    old = setup.store.get_attempt("a1")
    summary = setup.workflow.process("hw")
    assert summary["completed"] == 0
    services.ocr.extract.assert_not_called()
    services.grader.grade.assert_not_called()
    setup.client.download_attempt.assert_not_called()
    assert setup.store.get_attempt("a1") == old


def test_upload_of_old_review_is_blocked_before_network(setup):
    setup.store.approve("a1", 9, "", 10)
    setup.store.upsert_attempt("_200_1", "PB25000001", "hw", submitted_at="26-9-28")
    setup.client.reset_mock()
    with pytest.raises(ValueError, match="旧尝试.*最新提交"):
        setup.workflow.upload(["a1"])
    setup.client.list_assignments.assert_not_called()
    setup.client.upload_grade.assert_not_called()
    assert setup.store.get_attempt("a1")["status"] == "reviewed"


def test_explicit_ocr_reruns_and_invalidates_previous_ai(setup, services):
    setup.workflow.process("hw")
    services.ocr.extract.return_value = "纠正后的 OCR"
    setup.workflow.process("hw", "ocr", force_ocr=True)
    row = setup.store.get_attempt("a1")
    assert "纠正后的 OCR" in row["ocr_text"]
    assert row["ai_score"] is None
    assert row["status"] == "ocr_done"
    assert services.ocr.extract.call_count == 2


def test_failed_reocr_cannot_resume_stale_text_or_score(setup, services):
    setup.workflow.process("hw")
    services.ocr.extract.side_effect = ValueError("DOCX 内嵌图片失败")
    summary = setup.workflow.process("hw", "ocr", force_ocr=True)
    assert summary["failed"] == 1
    row = setup.store.get_attempt("a1")
    assert row["ocr_text"] == ""
    assert row["ai_score"] is None
    services.grader.grade.reset_mock()
    setup.workflow.process("hw", "grade")
    services.grader.grade.assert_not_called()
    services.ocr.extract.reset_mock()
    setup.workflow.process("hw", "all")
    services.ocr.extract.assert_called_once()
    services.grader.grade.assert_not_called()


def test_all_resumes_existing_ocr_without_redownload(setup, services):
    setup.workflow.process("hw")
    summary = setup.workflow.process("hw")
    assert setup.client.download_attempt.call_count == 1
    assert services.ocr.extract.call_count == 1
    assert services.grader.grade.call_count == 2
    assert summary["skipped_ocr"] == 1


@pytest.mark.parametrize("status", ["ocr_done", "graded", "error"])
def test_default_ocr_preserves_existing_text_scores_and_errors(setup, services, status):
    setup.workflow.process("hw")
    setup.store.update_attempt("a1", status=status, error="previous grading failure" if status == "error" else "")
    before = setup.store.get_attempt("a1")
    services.ocr_type.reset_mock()
    services.grade_type.reset_mock()
    services.ocr_type.side_effect = ValueError("invalid OCR settings must not be inspected")
    logs = []
    setup.workflow.log = logs.append

    summary = setup.workflow.process("hw", "ocr")

    assert setup.store.get_attempt("a1") == before
    assert summary["completed"] == summary["failed"] == 0
    assert summary["skipped_ocr"] == 1
    assert any("1 份提交已跳过识别" in message for message in logs)
    services.ocr_type.assert_not_called()
    services.grade_type.assert_not_called()


@pytest.mark.parametrize("stage", ["ocr", "all"])
def test_incremental_ocr_processes_whitespace_text_and_preflights_only_needed_files(setup, services, stage):
    setup.store.update_attempt("a1", paths=["cached.pdf"], ocr_text="已有识别结果", status="ocr_done")
    path = setup.root / "missing.txt"
    path.write_text("待识别", encoding="utf-8")
    setup.store.upsert_student("PB24000001", "另一学生")
    setup.store.upsert_attempt("a3", "PB24000001", "hw")
    setup.store.update_attempt("a3", paths=[str(path)], ocr_text=" \n\t", status="error", error="old failure")
    services.ocr.preflight.side_effect = ValueError("missing runtime is irrelevant to plain text")

    summary = setup.workflow.process("hw", stage)

    services.ocr.preflight.assert_not_called()
    services.ocr.extract.assert_called_once()
    assert services.ocr.extract.call_args.args[0] == path
    assert summary["skipped_ocr"] == 1
    assert summary["failed"] == 0
    assert summary["completed"] == (1 if stage == "ocr" else 2)
    assert setup.store.get_attempt("a3")["error"] == ""
    assert "学生作业识别文本" in setup.store.get_attempt("a3")["ocr_text"]


def test_all_resumes_cached_text_without_ocr_configuration_or_original_files(setup, services):
    setup.store.update_attempt("a1", ocr_text="已识别但已移除原始附件", status="error", error="grading failed")
    services.ocr_type.side_effect = ValueError("invalid OCR settings")
    setup.workflow.client = None

    summary = setup.workflow.process("hw", "all")

    assert summary["skipped_ocr"] == 1
    assert summary["completed"] == 1
    services.ocr_type.assert_not_called()
    setup.client.download_attempt.assert_not_called()
    services.grader.grade.assert_called_once()


@pytest.mark.parametrize("stage", ["download", "grade", "all"])
def test_force_ocr_cannot_be_requested_for_other_stages(setup, services, stage):
    with pytest.raises(ValueError, match="只适用于 OCR"):
        setup.workflow.process("hw", stage, force_ocr=True)
    services.ocr_type.assert_not_called()
    services.grade_type.assert_not_called()
    setup.client.download_attempt.assert_not_called()


@pytest.mark.parametrize("force_ocr", [False, True])
def test_incremental_and_forced_ocr_preserve_protected_and_out_of_scope_attempts(setup, services, force_ocr):
    setup.store.approve("a1", 9, "已经审核", 10)
    protected_ids = ["a1", "a2"]
    for index, status in enumerate(("uploaded", "uploading", "upload_uncertain", "bb_graded", "bb_reconciliation"), 10):
        student_id = f"PB2400{index:04}"
        setup.store.upsert_student(student_id, f"受保护示例学生 {index}")
        setup.store.upsert_attempt(status, student_id, "hw")
        setup.store.update_attempt(status, ocr_text="已有内容", status="ocr_done")
        if status.startswith("bb_"):
            setup.store.update_attempt(status, status=status)
        else:
            setup.store.approve(status, 9, "已经审核", 10)
            setup.store.begin_upload(status)
            if status == "uploaded":
                setup.store.mark_uploaded(status, {"verified": True})
            elif status == "upload_uncertain":
                setup.store.mark_upload_uncertain(status, "not confirmed")
        protected_ids.append(status)
    before = {attempt_id: setup.store.get_attempt(attempt_id) for attempt_id in protected_ids}

    summary = setup.workflow.process("hw", "ocr", force_ocr=force_ocr)

    assert {attempt_id: setup.store.get_attempt(attempt_id) for attempt_id in protected_ids} == before
    assert summary["completed"] == summary["failed"] == summary["skipped_ocr"] == 0
    services.ocr_type.assert_not_called()


def test_processing_failure_persists_error_without_fabricating_grade(setup, services):
    services.grader.grade.side_effect = RuntimeError("invalid model result")
    summary = setup.workflow.process("hw")
    assert summary["failed"] == 1
    row = setup.store.get_attempt("a1")
    assert row["status"] == "error"
    assert row["ai_score"] is None
    assert row["reviewed_score"] is None
    assert row["error"] == "invalid model result"


def test_login_injects_token_without_serializing_it(setup):
    setup.settings.secret = lambda name: "secret-bb-token" if name == "blackboard" else ""
    with patch("bb_assistant.workflow.BlackboardClient", return_value=setup.client) as client_type:
        setup.workflow.login("teacher", "password")
    assert client_type.call_args.kwargs["access_token"] == "secret-bb-token"
    assert "access_token" not in setup.settings.data["bb"]
    assert "secret-bb-token" not in (setup.root / "course_binding.json").read_text()


@pytest.mark.parametrize("stage", ["ocr", "grade"])
def test_offline_processing_refuses_mismatched_course_binding(setup, services, stage):
    binding = {key: setup.settings.data["bb"][key] for key in ("base_url", "course_id")}
    (setup.root / "course_binding.json").write_text(json.dumps(binding), encoding="utf-8")
    setup.settings.data["bb"]["course_id"] = "_different_course_1"
    setup.workflow.client = None
    with pytest.raises(ValueError, match="绑定另一课程"):
        setup.workflow.process("hw", stage)
    services.ocr.extract.assert_not_called()
    services.grader.grade.assert_not_called()


def test_processing_errors_redacted_in_database_logs_and_report(setup, services):
    secrets = {"ocr": "sensitive-ocr-key", "deepseek": "sensitive-ds-key", "blackboard": "sensitive-bb-key"}
    setup.settings.secret = lambda name: secrets.get(name, "")
    logs = []
    setup.workflow.log = logs.append
    services.grader.grade.side_effect = RuntimeError("provider echoed " + " ".join(secrets.values()))
    summary = setup.workflow.process("hw")
    saved = setup.store.get_attempt("a1")["error"]
    report = Path(summary["report_path"]).read_text(encoding="utf-8")
    for secret in secrets.values():
        assert secret not in saved
        assert secret not in "\n".join(logs)
        assert secret not in report
    assert saved.count("[REDACTED]") == 3


def test_upload_error_redacted_before_persistence_and_exception(setup):
    setup.settings.secret = lambda name: "upload-secret-token" if name == "blackboard" else ""
    setup.store.approve("a1", 9, "已审核", 10)
    setup.client.upload_grade.side_effect = RuntimeError("echo upload-secret-token")
    with pytest.raises(ValueError) as caught:
        setup.workflow.upload(["a1"])
    assert "upload-secret-token" not in str(caught.value)
    assert "upload-secret-token" not in setup.store.get_attempt("a1")["error"]
    assert caught.value.__suppress_context__


def test_failed_regrade_does_not_count_stale_ai_in_summary(setup, services):
    setup.workflow.process("hw")
    original_ocr = setup.store.get_attempt("a1")["provenance"]["ocr"]
    services.grader.grade.side_effect = RuntimeError("new grading failed")
    summary = setup.workflow.process("hw", "grade")
    row = setup.store.get_attempt("a1")
    assert row["ai_score"] is None
    assert row["ai_comment"] == row["rationale"] == ""
    assert row["uncertainties"] == []
    assert row["provenance"] == {"ocr": original_ocr, "source_sha256": setup.workflow._source_hash(row["paths"])}
    assert row["status"] == "error"
    assert summary["errors"] == 1
    assert summary["score_groups"] == []
    assert "score_mean" not in summary


def test_each_grade_run_uses_current_rubric_and_preserves_ocr_without_recognition(setup, services):
    setup.workflow.process("hw")
    old = setup.store.get_attempt("a1")
    services.ocr_type.reset_mock()
    services.grader.grade.reset_mock()
    setup.settings.data["rubric"].update(
        instructions="修订后的评分要求", reference_answer="修订后的参考解答", max_score=100,
    )
    current_rubric = copy.deepcopy(setup.settings.data["rubric"])

    def regrade(*args):
        # Invalidation happens before the potentially failing provider call.
        pending = setup.store.get_attempt("a1")
        assert pending["status"] == "ocr_done"
        assert pending["ai_score"] is None
        assert pending["provenance"] == {"ocr": old["provenance"]["ocr"], "source_sha256": old["provenance"]["source_sha256"]}
        return {"score": 95, "comment": "按照新标准评分", "rationale": "新评分依据", "uncertainties": []}

    services.grader.grade.side_effect = regrade
    first = setup.workflow.process("hw", "grade")
    second = setup.workflow.process("hw", "grade")

    assert first["completed"] == second["completed"] == 1
    assert services.grader.grade.call_count == 2
    assert all(call.args == (
        old["ocr_text"], current_rubric["instructions"], current_rubric["reference_answer"], 100.0,
    ) for call in services.grader.grade.call_args_list)
    services.ocr_type.assert_not_called()
    row = setup.store.get_attempt("a1")
    assert row["ocr_text"] == old["ocr_text"]
    assert row["provenance"]["ocr"] == old["provenance"]["ocr"]
    assert row["provenance"]["rubric"] == current_rubric
    assert row["ai_score"] == 95


@pytest.mark.parametrize("policy", ["missing", None, {"mode": "model"}])
def test_grading_without_error_count_policy_keeps_legacy_call_signature(setup, services, policy):
    rubric = setup.settings.data["rubric"]
    if policy == "missing":
        rubric.pop("scoring_policy", None)
    else:
        rubric["scoring_policy"] = policy
    setup.store.update_attempt("a1", ocr_text="已有识别文本", status="ocr_done")

    summary = setup.workflow.process("hw", "grade")

    assert summary["completed"] == 1
    services.grader.grade.assert_called_once_with(
        "已有识别文本", rubric["instructions"], rubric["reference_answer"], float(rubric["max_score"]),
    )
    services.ocr_type.assert_not_called()
    assert setup.store.get_attempt("a1")["provenance"]["rubric"] == rubric


def test_regrading_uses_current_policy_and_keeps_exact_snapshot_without_ocr(setup, services):
    setup.settings.data["rubric"].pop("scoring_policy", None)
    setup.workflow.process("hw")
    original = setup.store.get_attempt("a1")
    services.ocr_type.reset_mock()
    services.grader.grade.reset_mock()
    logs = []
    setup.workflow.log = logs.append
    policies = [
        {"mode": "error_count", "free_errors": 1, "deduction_per_error": 0.5, "unit": "major_question"},
        {"mode": "error_count", "free_errors": 0, "deduction_per_error": 0.25, "unit": "subquestion"},
        None,
    ]
    expected_scores = [10, 9.5, 8]

    for index, policy in enumerate(policies):
        setup.settings.data["rubric"]["scoring_policy"] = copy.deepcopy(policy)
        current_rubric = copy.deepcopy(setup.settings.data["rubric"])
        metadata = {"model": "fake-ds", "prompt_hash": f"policy-run-{index}"}
        if policy is not None:
            metadata.update(
                scoring_policy={**policy, "deduction_per_error": str(policy["deduction_per_error"])},
                wrong_question_count=index + 1, wrong_questions=[{"question_id": "1", "reason": "测试错误"}],
                score_calculation="本地计算表达式",
            )

        def regrade(*args, **kwargs):
            pending = setup.store.get_attempt("a1")
            assert pending["ai_score"] is None
            assert pending["provenance"] == {"ocr": original["provenance"]["ocr"], "source_sha256": original["provenance"]["source_sha256"]}
            assert args[0] == original["ocr_text"]
            assert kwargs == ({"scoring_policy": policy} if policy is not None else {})
            if policy is not None:
                # Provider normalization must not mutate the saved user policy.
                kwargs["scoring_policy"]["free_errors"] = 999
            services.grader.last_metadata = copy.deepcopy(metadata)
            return {"score": expected_scores[index], "comment": "测试评语", "rationale": "测试依据", "uncertainties": []}

        services.grader.grade.side_effect = regrade
        assert setup.workflow.process("hw", "grade")["completed"] == 1
        row = setup.store.get_attempt("a1")
        assert row["ai_score"] == expected_scores[index]
        assert row["provenance"]["rubric"] == current_rubric
        assert row["provenance"]["grading"] == metadata
        assert row["ocr_text"] == original["ocr_text"]
        assert row["provenance"]["ocr"] == original["provenance"]["ocr"]

    services.ocr_type.assert_not_called()
    assert services.grader.grade.call_count == 3
    mode_logs = [message for message in logs if message.startswith("评分方式：")]
    assert len(mode_logs) == 3
    assert "以大题为单位" in mode_logs[0]
    assert "以小题为单位" in mode_logs[1]
    assert "模型给出建议分数" in mode_logs[2]


def test_failed_error_count_regrade_removes_previous_calculation(setup, services):
    policy = {"mode": "error_count", "free_errors": 1, "deduction_per_error": 0.5, "unit": "major_question"}
    setup.settings.data["rubric"]["scoring_policy"] = policy
    services.grader.last_metadata = {
        "scoring_policy": policy, "wrong_question_count": 1, "score_calculation": "10 - 0 = 10",
    }
    setup.workflow.process("hw")
    original_ocr = setup.store.get_attempt("a1")["provenance"]["ocr"]
    services.ocr_type.reset_mock()
    services.grader.grade.side_effect = ValueError("缺少逐题判定，无法计算分数")

    summary = setup.workflow.process("hw", "grade")

    assert summary["failed"] == 1
    row = setup.store.get_attempt("a1")
    assert row["ai_score"] is None
    assert row["provenance"] == {"ocr": original_ocr, "source_sha256": setup.workflow._source_hash(row["paths"])}
    assert row["status"] == "error"
    services.ocr_type.assert_not_called()


def test_different_rubric_maxima_are_reported_as_separate_groups(setup, services):
    setup.workflow.process("hw")
    setup.store.approve("a1", 8, "保留原评分", 10)
    setup.store.upsert_student("PB24000001", "另一学生")
    setup.store.upsert_attempt("hundred", "PB24000001", "hw")
    setup.store.update_attempt("hundred", ocr_text="已有文本", ai_score=80,
                               provenance={"rubric": {"max_score": 100}}, status="graded")
    setup.store.approve("hundred", 80, "百分制旧评分", 100)
    summary = setup.workflow.process("hw", "grade")
    assert "score_mean" not in summary
    assert len(summary["score_groups"]) == 2
    assert summary["score_groups"][0]["score_mean"] == 8
    assert summary["score_groups"][1]["score_mean"] == 80


def test_zip_paths_are_reassigned_inside_output_and_docx_is_accepted(tmp_path):
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("../../escape.txt", "answer one")
        out.writestr("C:/absolute/path.pdf", "answer two")
        out.writestr("作业.docx", "answer three")
    output = tmp_path / "expanded"
    files = expand_documents([str(archive)], output)
    assert len(files) == 3
    assert all(path.is_relative_to(output) for path in files)
    assert {path.suffix for path in files} == {".txt", ".pdf", ".docx"}
    assert not (tmp_path / "escape.txt").exists()


def test_zip_rejects_executable_without_silently_skipping_it(tmp_path):
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("answer.pdf", "answer")
        out.writestr("run.exe", "not executed")
    with pytest.raises(ValueError, match="不支持"):
        expand_documents([str(archive)], tmp_path / "expanded")


def test_zip_rejects_symlink_entries(tmp_path):
    archive = tmp_path / "submission.zip"
    entry = zipfile.ZipInfo("answer.pdf")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(entry, "../../outside.pdf")
    with pytest.raises(ValueError, match="符号链接"):
        expand_documents([str(archive)], tmp_path / "expanded")


def test_zip_file_limit_is_aggregate_across_archives(tmp_path):
    archives = []
    for index in range(2):
        archive = tmp_path / f"submission-{index}.zip"
        with zipfile.ZipFile(archive, "w") as out:
            for number in range(101):
                out.writestr(f"{number}.txt", "answer")
        archives.append(str(archive))
    with pytest.raises(ValueError, match="200"):
        expand_documents(archives, tmp_path / "expanded")


def test_zip_uncompressed_size_limit_before_any_extraction(tmp_path):
    entry = zipfile.ZipInfo("large.pdf")
    entry.file_size = 200 * 1024 * 1024 + 1
    fake_archive = MagicMock()
    fake_archive.infolist.return_value = [entry]
    with patch("bb_assistant.workflow.zipfile.ZipFile") as archive_type:
        archive_type.return_value.__enter__.return_value = fake_archive
        with pytest.raises(ValueError, match="200 MB"):
            expand_documents([str(tmp_path / "large.zip")], tmp_path / "expanded")
    fake_archive.open.assert_not_called()


def test_safe_id_never_contains_student_controlled_paths():
    assert len(safe_id("../../中文/CON:attempt")) == 24
    assert set(safe_id("../../中文/CON:attempt")) <= set("0123456789abcdef")
