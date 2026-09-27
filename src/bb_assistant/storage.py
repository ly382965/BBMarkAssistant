"""Thread-friendly SQLite storage and the human-review boundary.

Each method owns its connection.  Publishing is deliberately a two-phase state
transition: a reviewed attempt can be claimed once, and an ambiguous network
result requires an explicit reconciliation before it can be tried again.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterator

from .attempts import latest_attempts


_STUDENT_ID = re.compile(r"PB[0-9]{8}\Z", re.ASCII)
_PROTECTED_STATUSES = {"reviewed", "uploading", "uploaded", "upload_uncertain"}
_FROZEN_STATUSES = {"uploading", "uploaded", "upload_uncertain"}
_LEGACY_PRE_SEND_FORM_ERROR = "BB 评分表单不是已识别的个人作业提交地址，已阻止写入。"
_DATA_FIELDS = {
    "paths", "ocr_text", "error", "ai_score", "ai_comment", "rationale",
    "uncertainties", "provenance", "status",
}
_RESULT_FIELDS = _DATA_FIELDS - {"error", "status"}
_JSON_FIELDS = {"paths": list, "uncertainties": list, "provenance": dict, "receipt": dict}
_SECRET_KEYS = {
    "apikey", "api_key", "password", "passwd", "secret", "client_secret",
    "authorization", "cookie", "cookies", "token", "access_token",
    "refresh_token", "credential", "credentials",
}


def normalize_student_id(student_id: str) -> str:
    value = str(student_id).strip().upper()
    if not value:
        raise ValueError("学号不能为空")
    return value


def is_in_scope(student_id: str) -> bool:
    """PB23/PB24 and PB25000001–PB25000262, inclusive; ASCII digits only."""
    value = str(student_id).strip().upper()
    return bool(_STUDENT_ID.fullmatch(value)) and (
        value.startswith(("PB23", "PB24"))
        or "PB25000001" <= value <= "PB25000262"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _metadata(value: Any) -> Any:
    """Keep audit/config metadata free of conventional credential fields."""
    if isinstance(value, dict):
        return {
            str(key): _metadata(item)
            for key, item in value.items()
            if str(key).lower().replace("-", "_") not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_metadata(item) for item in value]
    return value


def _finite_score(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数值")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    return number


def _normal_status(status: Any) -> str:
    if not isinstance(status, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", status):
        raise ValueError("处理状态必须是小写字母、数字或下划线")
    if status in _PROTECTED_STATUSES:
        raise ValueError("审核及上传状态只能通过专用方法修改")
    return status


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "assistant.sqlite3"
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS students (
                    student_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    bb_user_id TEXT NOT NULL DEFAULT '',
                    in_scope INTEGER NOT NULL CHECK(in_scope IN (0,1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    max_score REAL NOT NULL CHECK(max_score > 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    assignment_id TEXT NOT NULL REFERENCES assignments(id),
                    detail_url TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'submitted',
                    paths TEXT NOT NULL DEFAULT '[]',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    ai_score REAL,
                    ai_comment TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    uncertainties TEXT NOT NULL DEFAULT '[]',
                    provenance TEXT NOT NULL DEFAULT '{}',
                    reviewed_score REAL,
                    reviewed_comment TEXT,
                    reviewed_max_score REAL,
                    reviewed_at TEXT,
                    upload_started_at TEXT,
                    uploaded_at TEXT,
                    receipt TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_assignment ON attempts(assignment_id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_attempt ON audit(attempt_id, id);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for name in _JSON_FIELDS:
            if name in result:
                result[name] = json.loads(result[name])
        if "in_scope" in result:
            result["in_scope"] = bool(result["in_scope"])
        return result

    @staticmethod
    def _audit(conn: sqlite3.Connection, event: str, payload: dict[str, Any], attempt_id: str | None = None) -> None:
        clean = _metadata(payload)
        previous = conn.execute("SELECT entry_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else ""
        created_at = _now()
        entry_hash = _digest({"attempt_id": attempt_id, "event": event, "payload": clean,
                              "created_at": created_at, "previous_hash": previous_hash})
        conn.execute(
            "INSERT INTO audit(attempt_id,event,payload,created_at,previous_hash,entry_hash) VALUES(?,?,?,?,?,?)",
            (attempt_id, event, _json(clean), created_at, previous_hash, entry_hash),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = conn.execute("""
            SELECT a.*, s.name, s.bb_user_id, s.in_scope,
                   g.title AS assignment_title, g.max_score AS assignment_max_score
            FROM attempts a JOIN students s ON a.student_id=s.student_id
            JOIN assignments g ON a.assignment_id=g.id WHERE a.id=?
        """, (str(attempt_id),)).fetchone()
        if row is None:
            raise KeyError(f"找不到提交记录: {attempt_id}")
        return row

    def upsert_student(self, student_id: str, name: str, bb_user_id: str = "", in_scope: bool | None = None) -> None:
        student_id = normalize_student_id(student_id)
        scoped = is_in_scope(student_id) and (in_scope is None or bool(in_scope))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO students(student_id,name,bb_user_id,in_scope,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,
                  bb_user_id=CASE WHEN excluded.bb_user_id='' THEN students.bb_user_id ELSE excluded.bb_user_id END,
                  in_scope=excluded.in_scope,updated_at=excluded.updated_at
            """, (student_id, str(name), str(bb_user_id), int(scoped), _now()))
            self._audit(conn, "student_refreshed", {"student_id": student_id, "in_scope": scoped})

    def list_students(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [self._decode(row) for row in conn.execute("SELECT * FROM students ORDER BY student_id")]

    def upsert_assignment(self, id: str, title: str, max_score: float = 10) -> None:
        maximum = _finite_score(max_score, "满分")
        if maximum <= 0 or not str(id).strip():
            raise ValueError("作业 ID 不能为空，满分必须大于零")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO assignments(id,title,max_score,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,max_score=excluded.max_score,updated_at=excluded.updated_at
            """, (str(id), str(title), maximum, _now()))
            self._audit(conn, "assignment_refreshed", {"assignment_id": str(id), "max_score": maximum})

    def list_assignments(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM assignments ORDER BY title,id")]

    def upsert_attempt(self, id: str, student_id: str, assignment_id: str, detail_url: str = "",
                       submitted_at: str = "", status: str = "submitted") -> None:
        student_id = normalize_student_id(student_id)
        status = _normal_status(status)
        if not str(id).strip():
            raise ValueError("提交 ID 不能为空")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM attempts WHERE id=?", (str(id),)).fetchone()
            if old:
                if old["student_id"] != student_id or old["assignment_id"] != str(assignment_id):
                    raise ValueError("同一提交 ID 的学生或作业发生变化，拒绝覆盖")
                conn.execute("""
                    UPDATE attempts SET detail_url=CASE WHEN ?='' THEN detail_url ELSE ? END,
                      submitted_at=CASE WHEN ?='' THEN submitted_at ELSE ? END,updated_at=? WHERE id=?
                """, (detail_url, detail_url, submitted_at, submitted_at, _now(), str(id)))
            else:
                conn.execute("""
                    INSERT INTO attempts(id,student_id,assignment_id,detail_url,submitted_at,status,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                """, (str(id), student_id, str(assignment_id), str(detail_url), str(submitted_at), status, _now()))
            self._audit(conn, "attempt_refreshed" if old else "attempt_created",
                        {"student_id": student_id, "assignment_id": str(assignment_id)}, str(id))

    def list_attempts(self, assignment_id: str, *, latest_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT a.*, s.name, s.bb_user_id, s.in_scope,
                       g.title AS assignment_title, g.max_score AS assignment_max_score
                FROM attempts a JOIN students s ON a.student_id=s.student_id
                JOIN assignments g ON a.assignment_id=g.id
                WHERE a.assignment_id=? ORDER BY a.student_id,a.submitted_at,a.id
            """, (str(assignment_id),))
            decoded = [self._decode(row) for row in rows]
            return latest_attempts(decoded) if latest_only else decoded

    def get_attempt(self, id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._decode(self._row(conn, id))

    def update_attempt(self, id: str, **fields: Any) -> None:
        unknown = set(fields) - _DATA_FIELDS
        if unknown:
            raise ValueError(f"不可修改的字段: {', '.join(sorted(unknown))}")
        if not fields:
            return
        values = dict(fields)
        if "status" in values:
            values["status"] = _normal_status(values["status"])
        if "ai_score" in values and values["ai_score"] is not None:
            values["ai_score"] = _finite_score(values["ai_score"], "AI 分数")
            if values["ai_score"] < 0:
                raise ValueError("AI 分数不能小于零")
        for name, expected in _JSON_FIELDS.items():
            if name in values:
                if not isinstance(values[name], expected):
                    raise ValueError(f"{name} 必须为 {expected.__name__}")
                if name == "paths" and any(not isinstance(p, (str, Path)) for p in values[name]):
                    raise ValueError("paths 必须为路径列表")
                if name == "paths":
                    values[name] = [str(path) for path in values[name]]
                if name == "provenance":
                    values[name] = _metadata(values[name])
                values[name] = _json(values[name])
        for name in {"ocr_text", "error", "ai_comment", "rationale"} & values.keys():
            if not isinstance(values[name], str):
                raise ValueError(f"{name} 必须为字符串")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] in _FROZEN_STATUSES:
                raise ValueError("上传中、已上传或上传结果待核对的记录不能重新处理")
            invalidate = bool(_RESULT_FIELDS & fields.keys()) or "status" in fields
            if invalidate:
                values.update(reviewed_score=None, reviewed_comment=None, reviewed_max_score=None, reviewed_at=None)
                if "status" not in fields:
                    values["status"] = "scored" if "ai_score" in fields and fields["ai_score"] is not None else (
                        "ocr_done" if "ocr_text" in fields else "submitted")
            # Replacing downloaded material or OCR makes previous AI output stale.
            if ({"paths", "ocr_text"} & fields.keys()) and "ai_score" not in fields:
                values.update(ai_score=None, ai_comment="", rationale="", uncertainties="[]")
                if "provenance" not in fields:
                    values["provenance"] = "{}"
            if "paths" in fields and "ocr_text" not in fields:
                values["ocr_text"] = ""
            values["updated_at"] = _now()
            columns = ",".join(f"{name}=?" for name in values)
            conn.execute(f"UPDATE attempts SET {columns} WHERE id=?", (*values.values(), str(id)))
            self._audit(conn, "attempt_updated", {
                "fields": sorted(fields), "result_hash": _digest(values),
                "approval_invalidated": bool(invalidate and old["reviewed_at"]),
                "provenance": _metadata(fields.get("provenance", {})),
            }, str(id))

    def approve(self, id: str, score: float, comment: str, max_score: float) -> None:
        score = _finite_score(score, "审核分数")
        maximum = _finite_score(max_score, "满分")
        if maximum <= 0 or not 0 <= score <= maximum:
            raise ValueError("审核分数必须位于 0 和满分之间")
        if not isinstance(comment, str):
            raise ValueError("评语必须为字符串")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] in _FROZEN_STATUSES:
                raise ValueError("上传中、已上传或上传结果待核对的记录不能重新审核")
            now = _now()
            conn.execute("""
                UPDATE attempts SET reviewed_score=?,reviewed_comment=?,reviewed_max_score=?,reviewed_at=?,
                  status='reviewed',error='',updated_at=? WHERE id=?
            """, (score, comment, maximum, now, now, str(id)))
            self._audit(conn, "human_approved", {
                "score": score, "max_score": maximum, "comment_hash": _digest(comment),
                "ocr_hash": _digest(old["ocr_text"]), "provenance": json.loads(old["provenance"]),
            }, str(id))

    def begin_upload(self, id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] != "reviewed" or old["reviewed_at"] is None or old["reviewed_score"] is None:
                raise ValueError("只能上传已人工审核且尚未上传的记录")
            if not old["in_scope"] or not is_in_scope(old["student_id"]):
                raise ValueError("该学生不在指定批改范围内")
            if old["reviewed_max_score"] != old["assignment_max_score"]:
                raise ValueError("审核满分与 BB 作业满分不一致，请核对后重新审核")
            versions = [dict(row) for row in conn.execute(
                "SELECT id,student_id,assignment_id,submitted_at FROM attempts WHERE student_id=? AND assignment_id=?",
                (old["student_id"], old["assignment_id"]),
            )]
            if latest_attempts(versions)[0]["id"] != old["id"]:
                raise ValueError("该学生已有更新的提交；旧提交不能上传，请刷新并审核最新提交。")
            now = _now()
            conn.execute("UPDATE attempts SET status='uploading',upload_started_at=?,error='',updated_at=? WHERE id=?",
                         (now, now, str(id)))
            self._audit(conn, "upload_started", {
                "score": old["reviewed_score"], "reviewed_at": old["reviewed_at"],
                "comment_hash": _digest(old["reviewed_comment"]),
            }, str(id))
            return self._decode(self._row(conn, id))

    def revoke_review(self, id: str) -> None:
        """Explicitly return a reviewed attempt to the editable grading stage."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] != "reviewed":
                raise ValueError("只能撤销尚未上传的人工审核")
            status = "graded" if old["ai_score"] is not None else ("ocr_done" if old["ocr_text"] else "submitted")
            conn.execute("""
                UPDATE attempts SET reviewed_score=NULL,reviewed_comment=NULL,reviewed_max_score=NULL,
                  reviewed_at=NULL,status=?,updated_at=? WHERE id=?
            """, (status, _now(), str(id)))
            self._audit(conn, "human_review_revoked", {"previous_reviewed_at": old["reviewed_at"]}, str(id))

    def mark_uploaded(self, id: str, receipt: dict[str, Any]) -> None:
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError("上传成功必须附带 BB 确认信息")
        receipt = _metadata(receipt)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] != "uploading":
                raise ValueError("只有上传中的记录才能标记为上传成功")
            now = _now()
            conn.execute("UPDATE attempts SET status='uploaded',receipt=?,uploaded_at=?,error='',updated_at=? WHERE id=?",
                         (_json(receipt), now, now, str(id)))
            self._audit(conn, "upload_confirmed", {"receipt": receipt, "receipt_hash": _digest(receipt)}, str(id))

    def mark_upload_uncertain(self, id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] not in {"uploading", "upload_uncertain"}:
                raise ValueError("记录尚未开始上传")
            conn.execute("UPDATE attempts SET status='upload_uncertain',error=?,updated_at=? WHERE id=?",
                         (str(error), _now(), str(id)))
            self._audit(conn, "upload_uncertain", {"error_hash": _digest(str(error))}, str(id))

    def mark_upload_blocked_before_send(self, id: str, error: str) -> None:
        """Release a claimed review only when the adapter guarantees no grade write."""
        if not isinstance(error, str) or not error.strip():
            raise ValueError("写入前阻止上传必须保留具体原因")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] != "uploading":
                raise ValueError("只有上传中的记录才能标记为写入前阻止")
            conn.execute("""
                UPDATE attempts SET status='reviewed',upload_started_at=NULL,error=?,updated_at=? WHERE id=?
            """, (error, _now(), str(id)))
            self._audit(conn, "upload_blocked_before_send", {
                "error_hash": _digest(error), "grade_request_sent": False,
                "reviewed_at": old["reviewed_at"],
            }, str(id))

    def recover_legacy_presend_block(self, id: str, *, expected_error: str) -> None:
        """Repair only the old form-address rejection, proven to precede any write.

        This is a narrow migration, not a replacement for BB reconciliation.
        Other uncertain results must remain locked until explicitly checked.
        """
        if expected_error != _LEGACY_PRE_SEND_FORM_ERROR:
            raise ValueError("只能恢复已确认在发送前触发的旧版评分表单地址错误")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] != "upload_uncertain" or old["error"] != expected_error:
                raise ValueError("记录不是指定的旧版写入前阻止错误，不能自动解除锁定")
            if (not old["upload_started_at"] or not old["reviewed_at"]
                    or old["reviewed_score"] is None or old["reviewed_comment"] is None
                    or old["reviewed_max_score"] is None or old["uploaded_at"]
                    or json.loads(old["receipt"])):
                raise ValueError("旧版上传记录缺少完整审核状态或存在回执，不能自动恢复")
            # Refreshing the same attempt updates only download metadata. Every
            # other event must still end in exactly this failed upload round.
            events = list(conn.execute("""
                SELECT * FROM audit WHERE attempt_id=? AND event!='attempt_refreshed'
                ORDER BY id DESC LIMIT 2
            """, (str(id),)))
            if len(events) != 2 or [event["event"] for event in events] != ["upload_uncertain", "upload_started"]:
                raise ValueError("缺少对应的旧版上传开始和失败审计，不能自动恢复")
            uncertain, started = events
            started_payload = json.loads(started["payload"])
            uncertain_payload = json.loads(uncertain["payload"])
            if (uncertain_payload != {"error_hash": _digest(expected_error)}
                    or started_payload != {
                        "score": old["reviewed_score"], "reviewed_at": old["reviewed_at"],
                        "comment_hash": _digest(old["reviewed_comment"]),
                    }
                    or not old["upload_started_at"] <= started["created_at"] <= uncertain["created_at"]):
                raise ValueError("旧版错误、审核结果或上传轮次与审计不一致，不能自动恢复")
            conn.execute("""
                UPDATE attempts SET status='reviewed',upload_started_at=NULL,updated_at=? WHERE id=?
            """, (_now(), str(id)))
            self._audit(conn, "legacy_upload_blocked_before_send_recovered", {
                "error_hash": _digest(expected_error), "grade_request_sent": False,
                "upload_started_audit_id": started["id"], "upload_uncertain_audit_id": uncertain["id"],
                "reason": "旧版本将评分表单地址校验的写入前阻止误分类为上传结果不明",
            }, str(id))

    def reset_upload_after_check(self, id: str, *, confirmed_not_uploaded: bool = False, note: str = "") -> None:
        """Human-only reconciliation after checking BB; never call on a timer/retry."""
        if confirmed_not_uploaded is not True or not note.strip():
            raise ValueError("必须在 BB 人工确认未上传，并填写核对说明")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] not in {"uploading", "upload_uncertain"}:
                raise ValueError("只有上传未确认的记录需要人工解除锁定")
            conn.execute("""
                UPDATE attempts SET status='reviewed',upload_started_at=NULL,error='',updated_at=? WHERE id=?
            """, (_now(), str(id)))
            self._audit(conn, "upload_reset_after_human_check", {"note": note}, str(id))

    def confirm_uploaded_after_check(self, id: str, receipt: dict[str, Any], note: str) -> None:
        """Record the human's positive BB verification after an ambiguous write."""
        if not isinstance(receipt, dict) or receipt.get("verified") is not True or not note.strip():
            raise ValueError("必须提供人工核对确认信息及说明")
        clean_receipt = _metadata(receipt)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._row(conn, id)
            if old["status"] not in {"uploading", "upload_uncertain"}:
                raise ValueError("只有上传未确认的记录需要人工核对")
            now = _now()
            conn.execute("UPDATE attempts SET status='uploaded',receipt=?,uploaded_at=?,error='',updated_at=? WHERE id=?",
                         (_json(clean_receipt), now, now, str(id)))
            self._audit(conn, "upload_confirmed_after_human_check", {"receipt": clean_receipt, "note": note}, str(id))

    def list_audit(self, attempt_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT * FROM audit"
            args: tuple[str, ...] = ()
            if attempt_id is not None:
                query += " WHERE attempt_id=?"
                args = (str(attempt_id),)
            rows = conn.execute(query + " ORDER BY id", args)
            return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def export_report(self, assignment_id: str, path: Path, *, latest_only: bool = True) -> Path:
        path = Path(path).expanduser().resolve()
        if path.suffix.lower() not in {".csv", ".json"}:
            raise ValueError("报告扩展名必须是 .csv 或 .json")
        with self._connect() as conn:
            assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (str(assignment_id),)).fetchone()
            if assignment is None:
                raise KeyError(f"找不到作业: {assignment_id}")
        rows = self.list_attempts(assignment_id, latest_only=latest_only)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8",
                                             newline="", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as out:
                temporary = Path(out.name)
                if path.suffix.lower() == ".json":
                    json.dump({"assignment": dict(assignment), "exported_at": _now(), "attempts": rows},
                              out, ensure_ascii=False, indent=2, allow_nan=False)
                else:
                    columns = ["id", "student_id", "name", "in_scope", "assignment_title", "assignment_max_score",
                               "ai_score", "ai_comment", "reviewed_score", "reviewed_comment", "reviewed_max_score",
                               "reviewed_at", "status", "uploaded_at", "error"]
                    writer = csv.DictWriter(out, fieldnames=columns)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({key: self._csv_safe(row.get(key)) for key in columns})
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._audit(conn, "report_exported", {"assignment_id": str(assignment_id), "format": path.suffix.lower(),
                                                  "row_count": len(rows), "data_hash": _digest(rows)})
        return path

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
            return "'" + value
        return value
