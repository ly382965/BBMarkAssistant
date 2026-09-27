"""Pre-send failures never unlock writes whose remote outcome is unknown."""

import json

import pytest

from bb_assistant.storage import Store


LEGACY_ERROR = "BB 评分表单不是已识别的个人作业提交地址，已阻止写入。"


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    result.upsert_student("PB24000001", "测试学生")
    result.upsert_assignment("hw", "测试作业", 10)
    result.upsert_attempt("a1", "PB24000001", "hw")
    result.update_attempt("a1", ocr_text="保留的 OCR", ai_score=8, ai_comment="模型评语", status="graded")
    result.approve("a1", 9.5, "", 10)
    return result


def uncertain(store, error=LEGACY_ERROR):
    store.begin_upload("a1")
    store.mark_upload_uncertain("a1", error)


def test_presend_block_preserves_approved_content_and_permits_later_explicit_retry(store):
    before = store.get_attempt("a1")
    store.begin_upload("a1")
    store.mark_upload_blocked_before_send("a1", "不能识别提交地址")
    after = store.get_attempt("a1")
    for key, value in before.items():
        if key not in {"updated_at", "error"}:
            assert after[key] == value
    assert after["error"] == "不能识别提交地址"
    event = store.list_audit("a1")[-1]
    assert event["event"] == "upload_blocked_before_send"
    assert event["payload"]["grade_request_sent"] is False
    assert store.begin_upload("a1")["reviewed_comment"] == ""


@pytest.mark.parametrize("state", ["reviewed", "upload_uncertain", "uploaded"])
def test_presend_block_only_accepts_uploading(store, state):
    if state == "upload_uncertain":
        uncertain(store)
    elif state == "uploaded":
        store.begin_upload("a1")
        store.mark_uploaded("a1", {"verified": True})
    with pytest.raises(ValueError, match="只有上传中"):
        store.mark_upload_blocked_before_send("a1", "blocked")
    assert store.get_attempt("a1")["status"] == state


@pytest.mark.parametrize("error", ["", "  ", None])
def test_presend_block_requires_specific_error(store, error):
    store.begin_upload("a1")
    with pytest.raises(ValueError, match="具体原因"):
        store.mark_upload_blocked_before_send("a1", error)
    assert store.get_attempt("a1")["status"] == "uploading"


def test_legacy_form_recovery_requires_matching_round_and_preserves_review(store):
    before = store.get_attempt("a1")
    uncertain(store)
    store.upsert_attempt("a1", "PB24000001", "hw", detail_url="https://bb.test/new")
    restarted = Store(store.root)
    restarted.recover_legacy_presend_block("a1", expected_error=LEGACY_ERROR)
    after = restarted.get_attempt("a1")
    for key in ("reviewed_score", "reviewed_comment", "reviewed_max_score", "reviewed_at", "ocr_text", "ai_score"):
        assert after[key] == before[key]
    assert after["status"] == "reviewed"
    assert after["upload_started_at"] is None
    assert after["error"] == LEGACY_ERROR
    audit = restarted.list_audit("a1")
    assert audit[-1]["event"] == "legacy_upload_blocked_before_send_recovered"
    assert audit[-1]["payload"]["grade_request_sent"] is False
    assert not any("human_check" in event["event"] for event in audit)
    with pytest.raises(ValueError):
        restarted.recover_legacy_presend_block("a1", expected_error=LEGACY_ERROR)
    assert restarted.begin_upload("a1")["reviewed_score"] == 9.5


@pytest.mark.parametrize("error", ["timeout", LEGACY_ERROR + " ", "server did not confirm"])
def test_legacy_recovery_never_unlocks_other_errors(store, error):
    uncertain(store, error)
    with pytest.raises(ValueError):
        store.recover_legacy_presend_block("a1", expected_error=error)
    with pytest.raises(ValueError):
        store.recover_legacy_presend_block("a1", expected_error=LEGACY_ERROR)
    assert store.get_attempt("a1")["status"] == "upload_uncertain"


@pytest.mark.parametrize("state", ["reviewed", "uploading", "uploaded"])
def test_legacy_recovery_only_accepts_uncertain(store, state):
    if state != "reviewed":
        store.begin_upload("a1")
    if state == "uploaded":
        store.mark_uploaded("a1", {"verified": True})
    with pytest.raises(ValueError):
        store.recover_legacy_presend_block("a1", expected_error=LEGACY_ERROR)
    assert store.get_attempt("a1")["status"] == state


@pytest.mark.parametrize("tamper", [
    "missing_start", "missing_uncertain", "wrong_error_hash", "wrong_score", "wrong_comment",
    "wrong_review_time", "wrong_round", "receipt", "missing_approval", "second_uncertain",
])
def test_legacy_recovery_rejects_incomplete_or_mismatched_audit(store, tamper):
    uncertain(store)
    with store._connect() as conn:
        if tamper == "missing_start":
            conn.execute("DELETE FROM audit WHERE event='upload_started'")
        elif tamper == "missing_uncertain":
            conn.execute("DELETE FROM audit WHERE event='upload_uncertain'")
        elif tamper == "wrong_error_hash":
            conn.execute("UPDATE audit SET payload=? WHERE event='upload_uncertain'", ('{"error_hash":"bad"}',))
        elif tamper in {"wrong_score", "wrong_comment", "wrong_review_time"}:
            row = conn.execute("SELECT payload FROM audit WHERE event='upload_started'").fetchone()
            payload = json.loads(row[0])
            key, value = {
                "wrong_score": ("score", 10), "wrong_comment": ("comment_hash", "bad"),
                "wrong_review_time": ("reviewed_at", "old"),
            }[tamper]
            payload[key] = value
            conn.execute("UPDATE audit SET payload=? WHERE event='upload_started'", (json.dumps(payload),))
        elif tamper == "wrong_round":
            conn.execute("UPDATE attempts SET upload_started_at='2999-01-01' WHERE id='a1'")
        elif tamper == "receipt":
            conn.execute("UPDATE attempts SET receipt=? WHERE id='a1'", ('{"verified":true}',))
        elif tamper == "missing_approval":
            conn.execute("UPDATE attempts SET reviewed_at=NULL WHERE id='a1'")
    if tamper == "second_uncertain":
        store.mark_upload_uncertain("a1", LEGACY_ERROR)
    with pytest.raises(ValueError):
        store.recover_legacy_presend_block("a1", expected_error=LEGACY_ERROR)
    assert store.get_attempt("a1")["status"] == "upload_uncertain"
