"""Latest-submission selection and storage guards use synthetic data only."""

import copy
import csv
from itertools import permutations
import json

import pytest

from bb_assistant.attempts import latest_attempts
from bb_assistant.storage import Store


def attempt(identifier, timestamp="", *, student="PB25000001", assignment="hw", status="submitted"):
    return {"id": identifier, "submitted_at": timestamp, "student_id": student,
            "assignment_id": assignment, "status": status}


@pytest.mark.parametrize("older,newer", [
    ("26-9-9", "26-9-10"),
    ("26-9-30", "26-10-1"),
    ("2026-09-27 09:00:00", "2026-09-27 09:00:01"),
    ("2026-09-27T09:00:00.001+08:00", "2026-09-27T09:00:00.002+08:00"),
    ("2026-09-27T09:00:00+08:00", "2026-09-27T02:00:00Z"),
    ("2026-09-27 09:00", "2026-09-27T01:01:00+00:00"),
    ("2026年9月27日 9时5分1秒", "2026/9/27 9:5:2"),
    ("2026年9月27日 9时5分", "2026年9月27日 9时6分"),
    ("2026.9.27", "2026年9月28日"),
])
def test_parsed_time_precedes_numeric_id_and_incoming_order(older, newer):
    old = attempt("_100_1", older)
    new = attempt("_9_1", newer)
    for rows in ([old, new], [new, old]):
        assert latest_attempts(rows) == [new]


@pytest.mark.parametrize("timestamps", [
    ("26-9-27", "26-9-27"), ("", ""), ("invalid", "invalid"),
    ("26-9-27", ""), ("", "26-9-27"),
    ("2026-09-27T01:00:00Z", "2026-09-27 09:00"),
])
@pytest.mark.parametrize("identifiers", [("_9_1", "_10_1"), ("9", "10")])
def test_same_or_missing_time_uses_numeric_bb_id(timestamps, identifiers):
    old = attempt(identifiers[0], timestamps[0])
    new = attempt(identifiers[1], timestamps[1])
    for rows in ([old, new], [new, old]):
        assert latest_attempts(rows) == [new]


def test_grouping_does_not_mix_students_or_assignments_and_never_mutates_rows():
    rows = [attempt("_3_1", "26-9-27"), attempt("_2_1", "26-9-26"),
            attempt("_1_1", "26-9-25", student="PB24000001"),
            attempt("_4_1", "26-9-24", assignment="other")]
    original = copy.deepcopy(rows)
    for reordered in permutations(rows):
        result = latest_attempts(list(reordered))
        assert [row["id"] for row in result] == ["_1_1", "_3_1", "_4_1"]
        assert all(any(row is original_row for original_row in rows) for row in result)
    assert rows == original


@pytest.mark.parametrize("latest_status", ["bb_graded", "reviewed", "uploaded", "error"])
def test_latest_status_never_causes_fallback_to_old_ungraded_attempt(latest_status):
    old = attempt("_9_1", "26-9-27", status="submitted")
    new = attempt("_10_1", "26-9-27", status=latest_status)
    assert latest_attempts([old, new]) == [new]


@pytest.mark.parametrize("timestamps", [("", ""), ("26-9-27", "26-9-27"), ("bad-date", "26-9-27")])
def test_opaque_ids_with_ambiguous_time_fail_closed(timestamps):
    with pytest.raises(ValueError, match="无法确定最新提交"):
        latest_attempts([attempt("opaque-old", timestamps[0]), attempt("opaque-new", timestamps[1])])


def test_unambiguous_opaque_ids_and_single_missing_date_are_supported():
    old, new = attempt("opaque-old", "26-9-26"), attempt("opaque-new", "26-9-27")
    assert latest_attempts([new, old]) == [new]
    assert latest_attempts([attempt("opaque-only")])[0]["id"] == "opaque-only"
    assert latest_attempts([]) == []


def test_equivalent_numeric_ids_do_not_guess_between_different_attempt_identities():
    with pytest.raises(ValueError, match="无法确定最新提交"):
        latest_attempts([attempt("_10_1"), attempt("10")])


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path)
    db.upsert_student("PB25000001", "模拟学生")
    db.upsert_assignment("hw", "模拟作业", 10)
    db.upsert_attempt("_10_1", "PB25000001", "hw", submitted_at="26-9-27")
    db.upsert_attempt("_9_1", "PB25000001", "hw", submitted_at="26-9-27")
    return db


def test_storage_latest_view_keeps_old_data_and_raw_default(store):
    store.update_attempt("_9_1", ocr_text="old OCR", ai_score=8, status="graded")
    old = store.get_attempt("_9_1")
    audit_before = store.list_audit()
    assert {row["id"] for row in store.list_attempts("hw")} == {"_9_1", "_10_1"}
    assert [row["id"] for row in store.list_attempts("hw", latest_only=True)] == ["_10_1"]
    assert store.get_attempt("_9_1") == old
    assert store.list_audit() == audit_before


@pytest.mark.parametrize("extension", ["json", "csv"])
def test_report_defaults_to_latest_but_can_include_history(store, tmp_path, extension):
    def read_ids(path):
        if extension == "json":
            rows = json.loads(path.read_text(encoding="utf-8"))["attempts"]
        else:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        return {row["id"] for row in rows}

    current = store.export_report("hw", tmp_path / f"current.{extension}")
    history = store.export_report("hw", tmp_path / f"history.{extension}", latest_only=False)
    assert read_ids(current) == {"_10_1"}
    assert read_ids(history) == {"_9_1", "_10_1"}


def test_upload_claim_refuses_previously_reviewed_older_attempt_without_mutation(store):
    store.approve("_9_1", 8, "", 10)
    before = store.get_attempt("_9_1")
    audits = store.list_audit()
    with pytest.raises(ValueError, match="更新的提交"):
        store.begin_upload("_9_1")
    assert store.get_attempt("_9_1") == before
    assert store.list_audit() == audits
    store.approve("_10_1", 9, "", 10)
    assert store.begin_upload("_10_1")["status"] == "uploading"


def test_upload_claim_rechecks_new_version_added_after_review(store):
    store.approve("_10_1", 9, "", 10)
    store.upsert_attempt("_11_1", "PB25000001", "hw", submitted_at="26-9-28", status="bb_graded")
    with pytest.raises(ValueError, match="更新的提交"):
        store.begin_upload("_10_1")
    assert store.get_attempt("_10_1")["status"] == "reviewed"
    assert store.list_attempts("hw", latest_only=True)[0]["status"] == "bb_graded"
