"""Approval, persistence, and export tests; no BB or model credentials needed."""

from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bb_assistant.storage import Store, is_in_scope


class ScopeTests(unittest.TestCase):
    def test_exact_scope_and_boundaries(self):
        for student_id in ("PB23000000", "PB23999999", "PB24000000", "PB24999999",
                           "PB25000001", "PB25000262", " pb25000001 "):
            with self.subTest(student_id=student_id):
                self.assertTrue(is_in_scope(student_id))
        for student_id in ("PB25000000", "PB25000263", "PB22999999", "PB26000001",
                           "PB230001", "PB230000001", "PB23１２３４５６", "xPB23000001", ""):
            with self.subTest(student_id=student_id):
                self.assertFalse(is_in_scope(student_id))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.upsert_student("pb25000001", "张同学", "_user1")
        self.store.upsert_assignment("hw1", "作业一", 10)
        self.store.upsert_attempt("a1", "PB25000001", "hw1", "https://example.edu/a1")

    def scored(self):
        self.store.update_attempt("a1", ocr_text="解答", status="ocr_done")
        self.store.update_attempt("a1", ai_score=8.5, ai_comment="思路正确", rationale="最后一步有误",
                                  provenance={"model": "test", "prompt_hash": "abc"}, status="scored")

    def approved(self):
        self.scored()
        self.store.approve("a1", 9, "已核对", 10)

    def test_roster_keeps_all_students_but_scope_cannot_be_bypassed(self):
        self.store.upsert_student("PB25000263", "范围外", in_scope=True)
        self.store.upsert_student("PB23000001", "手动排除", in_scope=False)
        students = {row["student_id"]: row for row in self.store.list_students()}
        self.assertEqual(len(students), 3)
        self.assertEqual(students["PB25000001"]["name"], "张同学")
        self.assertFalse(students["PB25000263"]["in_scope"])
        self.assertFalse(students["PB23000001"]["in_scope"])

    def test_refresh_preserves_review_and_files_across_restart(self):
        self.store.update_attempt("a1", paths=[self.root / "解答.pdf"])
        self.approved()
        Store(self.root).upsert_attempt("a1", "pb25000001", "hw1", "https://example.edu/new", status="submitted")
        row = Store(self.root).get_attempt("a1")
        self.assertEqual(row["paths"], [str(self.root / "解答.pdf")])
        self.assertEqual(row["status"], "reviewed")
        self.assertEqual(row["reviewed_score"], 9)
        self.assertEqual(row["reviewed_max_score"], 10)
        self.assertEqual(row["provenance"]["model"], "test")
        self.assertEqual(row["detail_url"], "https://example.edu/new")

    def test_identity_collision_is_refused_without_losing_progress(self):
        self.approved()
        self.store.upsert_student("PB24000001", "另一个人")
        self.store.upsert_assignment("hw2", "作业二")
        with self.assertRaises(ValueError):
            self.store.upsert_attempt("a1", "PB24000001", "hw1")
        with self.assertRaises(ValueError):
            self.store.upsert_attempt("a1", "PB25000001", "hw2")
        self.assertEqual(self.store.get_attempt("a1")["status"], "reviewed")

    def test_new_ai_result_invalidates_review_even_if_same_score(self):
        self.approved()
        self.store.update_attempt("a1", ai_score=8.5, ai_comment="重新生成", status="scored")
        row = self.store.get_attempt("a1")
        for field in ("reviewed_score", "reviewed_comment", "reviewed_at", "reviewed_max_score"):
            self.assertIsNone(row[field])
        with self.assertRaises(ValueError):
            self.store.begin_upload("a1")

    def test_new_ocr_invalidates_ai_and_review(self):
        self.approved()
        self.store.update_attempt("a1", ocr_text="新 OCR", provenance={"engine": "mineru"})
        row = self.store.get_attempt("a1")
        self.assertIsNone(row["ai_score"])
        self.assertIsNone(row["reviewed_score"])
        self.assertEqual(row["ai_comment"], "")
        self.assertEqual(row["provenance"], {"engine": "mineru"})

    def test_new_download_invalidates_all_derived_data(self):
        self.approved()
        self.store.update_attempt("a1", paths=["new.pdf"], status="downloaded")
        row = self.store.get_attempt("a1")
        self.assertEqual(row["ocr_text"], "")
        self.assertIsNone(row["ai_score"])
        self.assertIsNone(row["reviewed_score"])

    def test_cannot_bypass_approval_with_generic_update(self):
        self.scored()
        for fields in ({"status": "reviewed"}, {"status": "uploaded"}, {"reviewed_score": 10},
                       {"error = 'x'; DROP TABLE students; --": "x"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.update_attempt("a1", **fields)
        with self.assertRaises(ValueError):
            self.store.begin_upload("a1")
        self.assertEqual(len(self.store.list_students()), 1)

    def test_nonfinite_or_out_of_range_review_rejected(self):
        for score, maximum in ((float("nan"), 10), (float("inf"), 10), (-1, 10), (11, 10),
                               (0, 0), (5, float("inf")), (True, 10)):
            with self.subTest(score=score, maximum=maximum), self.assertRaises(ValueError):
                self.store.approve("a1", score, "", maximum)
        self.store.approve("a1", 0, "", 10)
        self.assertEqual(self.store.begin_upload("a1")["reviewed_score"], 0)

    def test_empty_final_comment_persists_and_is_claimed_without_ai_fallback(self):
        self.scored()
        self.store.approve("a1", 9, "", 10)
        restarted = Store(self.root)
        row = restarted.get_attempt("a1")
        self.assertEqual(row["status"], "reviewed")
        self.assertEqual(row["reviewed_comment"], "")
        self.assertEqual(row["ai_comment"], "思路正确")
        claimed = restarted.begin_upload("a1")
        self.assertEqual(claimed["reviewed_score"], 9)
        self.assertEqual(claimed["reviewed_comment"], "")
        self.assertEqual(claimed["status"], "uploading")

    def test_out_of_scope_review_cannot_be_uploaded(self):
        self.store.upsert_student("PB25000263", "范围外")
        self.store.upsert_attempt("outside", "PB25000263", "hw1")
        self.store.approve("outside", 9, "人工评分", 10)
        with self.assertRaises(ValueError):
            self.store.begin_upload("outside")

    def test_assignment_max_change_blocks_stale_review(self):
        self.approved()
        self.store.upsert_assignment("hw1", "作业一", 100)
        with self.assertRaises(ValueError):
            self.store.begin_upload("a1")

    def test_only_one_concurrent_upload_can_claim_review(self):
        self.approved()

        def claim():
            try:
                self.store.begin_upload("a1")
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: claim(), range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.get_attempt("a1")["status"], "uploading")

    def test_uncertain_upload_stays_locked_across_restart(self):
        self.approved()
        self.store.begin_upload("a1")
        self.store.mark_upload_uncertain("a1", "timeout after sending request")
        restarted = Store(self.root)
        self.assertEqual(restarted.get_attempt("a1")["status"], "upload_uncertain")
        with self.assertRaises(ValueError):
            restarted.begin_upload("a1")
        with self.assertRaises(ValueError):
            restarted.approve("a1", 10, "", 10)
        with self.assertRaises(ValueError):
            restarted.update_attempt("a1", status="scored")
        with self.assertRaises(ValueError):
            restarted.reset_upload_after_check("a1")
        restarted.reset_upload_after_check("a1", confirmed_not_uploaded=True, note="已在 BB 查看，仍无成绩")
        self.assertEqual(restarted.begin_upload("a1")["status"], "uploading")

    def test_uploaded_receipt_persists_and_cannot_be_repeated(self):
        self.approved()
        self.store.begin_upload("a1")
        self.store.mark_uploaded("a1", {"confirmed_score": 9, "bb_id": "_1"})
        row = Store(self.root).get_attempt("a1")
        self.assertEqual(row["status"], "uploaded")
        self.assertEqual(row["receipt"]["confirmed_score"], 9)
        self.assertIsNotNone(row["uploaded_at"])
        with self.assertRaises(ValueError):
            self.store.begin_upload("a1")

    def test_revoke_review_keeps_ai_and_blocks_upload(self):
        self.approved()
        self.store.revoke_review("a1")
        row = self.store.get_attempt("a1")
        self.assertEqual(row["status"], "graded")
        self.assertEqual(row["ai_score"], 8.5)
        self.assertIsNone(row["reviewed_at"])
        self.assertIsNone(row["reviewed_max_score"])
        with self.assertRaises(ValueError):
            self.store.begin_upload("a1")

    def test_revoke_does_not_unlock_an_ambiguous_upload(self):
        self.approved()
        self.store.begin_upload("a1")
        self.store.mark_upload_uncertain("a1", "timeout")
        with self.assertRaises(ValueError):
            self.store.revoke_review("a1")

    def test_human_can_confirm_an_ambiguous_write_after_bb_check(self):
        self.approved()
        self.store.begin_upload("a1")
        self.store.mark_upload_uncertain("a1", "timeout")
        with self.assertRaises(ValueError):
            self.store.confirm_uploaded_after_check("a1", {"verified": False}, "已检查")
        self.store.confirm_uploaded_after_check("a1", {"verified": True, "manually_confirmed": True}, "已核对 BB 分数和评语")
        self.assertEqual(self.store.get_attempt("a1")["status"], "uploaded")
        self.assertEqual(self.store.list_audit("a1")[-1]["event"], "upload_confirmed_after_human_check")

    def test_metadata_does_not_persist_conventional_credentials(self):
        self.store.update_attempt("a1", provenance={"model": "ds", "api_key": "SECRET_VALUE",
                                  "config": {"Authorization": "BEARER_VALUE", "base_url": "https://test"}})
        row = self.store.get_attempt("a1")
        self.assertEqual(row["provenance"], {"model": "ds", "config": {"base_url": "https://test"}})
        serialized = json.dumps(self.store.list_audit())
        self.assertNotIn("SECRET_VALUE", serialized)
        self.assertNotIn("BEARER_VALUE", serialized)

    def test_sql_parameters_and_csv_formula_neutralization(self):
        tricky = "=HYPERLINK(\"https://example.com\")"
        self.store.upsert_student("PB25000001", tricky)
        self.store.update_attempt("a1", ai_comment=" @SUM(1,2)", status="scored")
        self.store.approve("a1", 7, "\t=1+1", 10)
        assignment_id = "x'; DROP TABLE students; --"
        self.store.upsert_assignment(assignment_id, "安全参数")
        self.assertEqual(len(self.store.list_assignments()), 2)
        target = self.store.export_report("hw1", self.root / "report.csv")
        self.assertEqual(target.read_bytes()[:3], b"\xef\xbb\xbf")
        with target.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["name"], "'" + tricky)
        self.assertEqual(row["ai_comment"], "' @SUM(1,2)")
        self.assertEqual(row["reviewed_comment"], "'\t=1+1")
        self.assertEqual(row["reviewed_score"], "7.0")
        self.assertEqual(len(self.store.list_students()), 1)

    def test_json_report_and_audit_chain(self):
        self.approved()
        report = self.store.export_report("hw1", self.root / "report.json")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["attempts"][0]["name"], "张同学")
        self.assertEqual(data["attempts"][0]["reviewed_score"], 9)
        previous = ""
        for entry in self.store.list_audit():
            self.assertEqual(entry["previous_hash"], previous)
            content = {key: entry[key] for key in ("attempt_id", "event", "payload", "created_at", "previous_hash")}
            digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            self.assertEqual(entry["entry_hash"], digest)
            previous = entry["entry_hash"]

    def test_foreign_keys_prevent_orphan_attempts(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_attempt("orphan", "PB24000001", "hw1")


if __name__ == "__main__":
    unittest.main()
