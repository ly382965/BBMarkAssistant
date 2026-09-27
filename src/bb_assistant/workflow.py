from __future__ import annotations

import hashlib
import json
import copy
import math
import stat
import threading
import zipfile
from dataclasses import asdict
from pathlib import Path

from .attempts import latest_attempts
from .blackboard import Attempt, BlackboardClient, GradeUploadBlockedError
from .services import GradingClient, OcrClient
from .storage import Store, is_in_scope


def safe_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def expand_documents(paths: list[str], output: Path) -> list[Path]:
    """Extract document-only archives with bounded size; never execute submissions."""
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".txt", ".md", ".docx"}
    result = []
    extracted_bytes = 0
    archived_files = 0
    for raw in paths:
        path = Path(raw)
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                entries = [i for i in archive.infolist() if not i.is_dir()]
                archived_files += len(entries)
                if archived_files > 200 or sum(i.file_size for i in entries) + extracted_bytes > 200 * 1024 * 1024:
                    raise ValueError("压缩包超过 200 个文件或 200 MB，请人工检查。")
                for entry in entries:
                    if stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError("压缩包包含符号链接，请人工整理后处理。")
                    if Path(entry.filename).suffix.lower() not in allowed:
                        raise ValueError(f"压缩包包含不支持的文件：{entry.filename}；请人工整理后处理。")
                for i, entry in enumerate(entries):
                    suffix = Path(entry.filename).suffix.lower()
                    dest = output / safe_id(str(path)) / f"{i:03d}{suffix}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Reassign filenames; archive-controlled paths never reach filesystem.
                    with archive.open(entry) as source, dest.open("wb") as target:
                        while chunk := source.read(1024 * 1024):
                            extracted_bytes += len(chunk)
                            if extracted_bytes > 200 * 1024 * 1024:
                                raise ValueError("压缩包解压体积过大。")
                            target.write(chunk)
                    result.append(dest)
        elif path.suffix.lower() in allowed:
            result.append(path)
        else:
            raise ValueError(f"暂不支持附件 {path.name}，请人工检查，未跳过此附件评分。")
    if not result:
        raise ValueError("没有可识别的作业文件。")
    return result


class Workflow:
    def __init__(self, settings, store: Store, demo=False):
        self.settings, self.store, self.demo = settings, store, demo
        self.client = None
        self.cancel = threading.Event()
        self.log = lambda message: None

    def _bound(self):
        config = self.settings.data["bb"]
        identity = {k: config[k] for k in ("base_url", "course_id")}
        path = self.settings.root / "course_binding.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != identity:
            raise ValueError("数据目录已绑定另一课程。请用 --data-dir 指定新的目录，避免混用成绩。")
        return path, identity

    def redact_error(self, error: Exception | str) -> str:
        """Sanitize before persistence as well as before display/export."""
        message = str(error)
        secrets = {self.settings.secret(name) for name in ("blackboard", "ocr", "deepseek")}
        for secret in sorted((value for value in secrets if value), key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        return message

    def login(self, username: str, password: str):
        if self.demo:
            raise ValueError("演示模式不连接真实 BB。请启动普通模式登录。")
        path, identity = self._bound()
        self.client = None
        config = dict(self.settings.data["bb"])
        access_token = self.settings.secret("blackboard")
        if access_token:
            config["access_token"] = access_token
        client = BlackboardClient(**config)
        client.login(username, password)
        path.write_text(json.dumps(identity), encoding="utf-8")
        self.client = client
        self.log("BB 登录成功，正在同步课程名单和作业。")
        self.sync()

    def require_client(self):
        self._bound()
        if self.client is None:
            raise ValueError("请先在“连接与设置”中登录 BB。")
        return self.client

    def sync(self):
        client = self.require_client()
        students = client.list_students()
        assignments = client.list_assignments()
        for item in students:
            self.store.upsert_student(item.student_id, item.name, item.bb_user_id)
        for item in assignments:
            self.store.upsert_assignment(item.id, item.title, item.max_score)
        self.log(f"已同步 {len(students)} 名学生、{len(assignments)} 项作业。")

    def sync_attempts(self, assignment_id):
        for item in self.require_client().list_attempts(assignment_id):
            fields = asdict(item)
            # Remote grading status is not local human review or local AI output.
            remote_status = str(fields.get("status", "")).replace("_", "").lower()
            if remote_status in {"inprogress", "inprogressagain", "ip"}:
                continue
            if remote_status in {"completed", "graded"}:
                fields["status"] = "bb_graded"
            elif remote_status in {"needsreconciliation", "nr"}:
                fields["status"] = "bb_reconciliation"
            elif remote_status in {"submitted", "needsgrading", "ng"}:
                fields["status"] = "submitted"
            else:
                raise ValueError(f"BB 返回无法识别的提交状态：{fields.get('status')!r}，请人工检查。")
            self.store.upsert_attempt(**fields)
        self.log("作业提交记录已同步。每名学生默认使用最新提交，旧尝试保留在本地记录中。")

    @staticmethod
    def as_attempt(row):
        return Attempt(**{k: row.get(k, "") for k in ("id", "student_id", "assignment_id", "detail_url", "submitted_at")})

    def process(self, assignment_id, stage="all", *, force_ocr=False):
        if stage not in {"download", "ocr", "grade", "all"}:
            raise ValueError("未知处理阶段。")
        if force_ocr and stage != "ocr":
            raise ValueError("强制重新识别只适用于 OCR 阶段。")
        if self.demo:
            self.log("演示数据已包含 OCR 和建议分数；真实处理请在普通模式配置服务。")
            return
        self._bound()
        all_rows = self.store.list_attempts(assignment_id)
        rows = latest_attempts(all_rows)
        ignored_old = sum(row["in_scope"] for row in all_rows) - sum(row["in_scope"] for row in rows)
        if ignored_old:
            self.log(f"已忽略 {ignored_old} 份旧尝试，每名学生仅处理最新提交。")
        protected_statuses = {"reviewed", "uploaded", "uploading", "upload_uncertain", "bb_graded", "bb_reconciliation"}
        eligible = [row for row in rows if row["in_scope"] and row["status"] not in protected_statuses]
        ocr_rows = [
            row for row in eligible
            if stage in {"ocr", "all"} and (force_ocr or not (row.get("ocr_text") or "").strip())
        ]
        skipped_ocr = len(eligible) - len(ocr_rows) if stage in {"ocr", "all"} else 0
        if skipped_ocr:
            self.log(f"已有 OCR 文本的 {skipped_ocr} 份提交已跳过识别，保留现有结果。")
        ocr = OcrClient(self.settings.data["ocr"], self.settings.secret("ocr")) if ocr_rows else None
        if ocr is not None:
            ocr.progress = self.log
            ocr.cancel_requested = self.cancel.is_set
        grader = GradingClient(self.settings.data["grading"], self.settings.secret("deepseek")) if stage in {"grade", "all"} else None
        rubric = copy.deepcopy(self.settings.data["rubric"])
        if stage in {"grade", "all"} and not rubric["instructions"].strip():
            raise ValueError("请先填写评分标准。")
        scoring_policy = rubric.get("scoring_policy")
        if not isinstance(scoring_policy, dict) or scoring_policy.get("mode") != "error_count":
            scoring_policy = None
        if stage in {"grade", "all"}:
            if scoring_policy is not None:
                unit = "大题" if scoring_policy.get("unit") == "major_question" else "小题"
                self.log(f"评分方式：按错题数在本地计算分数，以{unit}为单位；"
                         f"前 {scoring_policy.get('free_errors')} 道错题不扣分，"
                         f"之后每道错题扣 {scoring_policy.get('deduction_per_error')} 分。")
            else:
                self.log("评分方式：按照评分标准由模型给出建议分数。")
        if ocr is not None and any(
            not row.get("paths") or any(Path(path).suffix.lower() not in {".txt", ".md"} for path in row["paths"])
            for row in ocr_rows
        ):
            ocr.preflight()
            mode = self.settings.data["ocr"].get("mode", "command")
            self.log("OCR 批次检查通过：本地命令模式。" if mode == "command" else f"OCR 模式：{mode}，将使用配置的 API 服务。")
        completed = failed = 0
        for row in eligible:
            if self.cancel.is_set():
                break
            if stage == "ocr" and not force_ocr and (row.get("ocr_text") or "").strip():
                continue
            task_dir = self.settings.root / "submissions" / safe_id(row["id"])
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.log(f"{row['student_id']} · {row['name']}：开始处理")
                paths = row.get("paths") or []
                if stage == "download" or (stage == "all" and not paths and not (row.get("ocr_text") or "").strip()):
                    paths = [str(p) for p in self.require_client().download_attempt(self.as_attempt(row), task_dir)]
                    self.store.update_attempt(row["id"], paths=paths, status="downloaded", error="")
                    row = self.store.get_attempt(row["id"])
                text = row.get("ocr_text") or ""
                if stage in {"ocr", "all"} and (force_ocr or not text.strip()):
                    # A failed re-read must not leave old partial text or AI
                    # scores eligible for a later resume/grade operation.
                    self.store.update_attempt(row["id"], ocr_text="", status="downloaded", error="")
                    documents = expand_documents(paths, task_dir / "extracted")
                    pieces = []
                    ocr_metadata = []
                    for document in documents:
                        if self.cancel.is_set():
                            raise ValueError("已取消，当前作业未评分。")
                        pieces.append(f"\n--- 附件 {document.name} ---\n" + ocr.extract(document, task_dir / "ocr" / safe_id(str(document))))
                        ocr_metadata.append(dict(getattr(ocr, "last_metadata", {})))
                    text = "\n".join(pieces)
                    self.store.update_attempt(row["id"], ocr_text=text, status="ocr_done", error="", provenance={"ocr": ocr_metadata})
                if stage in {"grade", "all"}:
                    if self.cancel.is_set():
                        break
                    provenance = self.store.get_attempt(row["id"]).get("provenance") or {}
                    for key in ("grading", "rubric", "input_sha256"):
                        provenance.pop(key, None)
                    # A failed request with a revised rubric must not present
                    # the previous score as a result of the new grading run.
                    self.store.update_attempt(
                        row["id"], ai_score=None, ai_comment="", rationale="", uncertainties=[],
                        provenance=provenance, status="ocr_done", error="",
                    )
                    if not text.strip():
                        raise ValueError("尚无 OCR 文本，请先下载并识别。")
                    grading_options = {"scoring_policy": copy.deepcopy(scoring_policy)} if scoring_policy is not None else {}
                    result = grader.grade(text, rubric["instructions"], rubric["reference_answer"],
                                          float(rubric["max_score"]), **grading_options)
                    provenance.update({"grading": getattr(grader, "last_metadata", {}), "rubric": rubric,
                                       "input_sha256": hashlib.sha256(text.encode()).hexdigest()})
                    self.store.update_attempt(row["id"], ai_score=result["score"], ai_comment=result["comment"],
                                              rationale=result["rationale"], uncertainties=result["uncertainties"],
                                              provenance=provenance, status="graded", error="")
                completed += 1
            except Exception as exc:
                failed += 1
                error = self.redact_error(exc)
                self.store.update_attempt(row["id"], status="error", error=error)
                self.log(f"{row['student_id']}：{error}")
        self.log(f"本批次完成 {completed} 份，失败 {failed} 份。" + (" 已取消。" if self.cancel.is_set() else ""))
        current = [item for item in self.store.list_attempts(assignment_id, latest_only=True) if item["in_scope"]]
        score_groups: dict[float | None, list[float]] = {}
        for item in current:
            if item["status"] not in {"graded", "reviewed", "uploaded"} or item["ai_score"] is None:
                continue
            source_max = (item.get("provenance") or {}).get("rubric", {}).get("max_score")
            try:
                source_max = float(source_max)
                if not math.isfinite(source_max) or source_max <= 0:
                    source_max = None
            except (TypeError, ValueError, OverflowError):
                source_max = None
            score_groups.setdefault(source_max, []).append(item["ai_score"])
        summary = {
            "completed": completed, "failed": failed, "skipped_ocr": skipped_ocr,
            "ignored_old_attempts": ignored_old, "in_scope_attempts": len(current),
            "pending": sum(item["status"] in {"submitted", "downloaded", "ocr_done"} for item in current),
            "awaiting_review": sum(item["status"] == "graded" for item in current),
            "reviewed": sum(item["status"] == "reviewed" for item in current),
            "errors": sum(item["status"] == "error" for item in current),
        }
        self.log(f"当前范围内 {len(current)} 份提交：待处理 {summary['pending']}，待审核 {summary['awaiting_review']}，"
                 f"已审核 {summary['reviewed']}，失败 {summary['errors']}。")
        summary["score_groups"] = []
        if len(score_groups) > 1 or None in score_groups:
            self.log("建议分数存在不同满分或缺少评分尺度，已按原评分满分分组；不计算混合平均分。")
        for source_max, scores in sorted(score_groups.items(), key=lambda pair: (pair[0] is None, pair[0] or 0)):
            group = {"max_score": source_max, "count": len(scores)}
            if source_max is None:
                self.log(f"建议分数（{len(scores)} 份）：原评分满分未知，未计算统计值。")
            else:
                stats = {"score_min": min(scores), "score_max": max(scores), "score_mean": sum(scores) / len(scores)}
                group.update(stats)
                self.log(f"建议分数（满分 {source_max:g}，{len(scores)} 份）：最低 {min(scores):g}，最高 {max(scores):g}，平均 {sum(scores) / len(scores):.2f}。")
                if len(score_groups) == 1:
                    summary.update(stats)
            summary["score_groups"].append(group)
        report = self.store.export_report(assignment_id, self.settings.root / "reports" / f"{safe_id(assignment_id)}-latest.json")
        summary["report_path"] = str(report)
        self.log(f"批改报告已保存：{report}")
        return summary

    def upload(self, ids: list[str]):
        if self.demo:
            raise ValueError("演示模式禁止上传成绩。")
        if not ids:
            return
        if len(ids) != len(set(ids)):
            raise ValueError("同一提交不能在一个批次中重复上传。")
        rows = [self.store.get_attempt(attempt_id) for attempt_id in ids]
        if len({(row["student_id"], row["assignment_id"]) for row in rows}) != len(rows):
            raise ValueError("同一学生的同一作业只能选择一份提交回传，请核对提交版本。")
        latest_ids = {
            item["id"] for assignment_id in {row["assignment_id"] for row in rows}
            for item in self.store.list_attempts(assignment_id, latest_only=True)
        }
        if any(row["id"] not in latest_ids for row in rows):
            raise ValueError("所选记录包含旧尝试，只能回传每名学生的最新提交；请刷新列表并审核最新提交。")
        maximum = float(self.settings.data["rubric"]["max_score"])
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("当前评分满分必须是大于零的有限数值。")
        for row in rows:
            if row["status"] != "reviewed" or row["reviewed_max_score"] is None:
                raise ValueError(f"{row['student_id']}：只能上传已人工审核且尚未上传的记录。")
            if not row["in_scope"] or not is_in_scope(row["student_id"]):
                raise ValueError(f"{row['student_id']}：该学生不在指定批改范围内。")
            if not math.isclose(maximum, row["reviewed_max_score"], rel_tol=0, abs_tol=0.0001):
                raise ValueError(f"{row['student_id']}：当前评分满分与人工审核时不同，请重新审核。")
        client = self.require_client()
        assignment_map = {a.id: a for a in client.list_assignments()}
        for row in rows:
            actual = assignment_map.get(row["assignment_id"])
            if actual is None or not math.isfinite(float(actual.max_score)) or not math.isclose(float(actual.max_score), row["reviewed_max_score"], rel_tol=0, abs_tol=0.0001):
                raise ValueError(f"{row['student_id']}：BB 作业满分与人工审核满分不一致；请核对评分尺度后重新审核。")
            self.store.upsert_assignment(actual.id, actual.title, actual.max_score)
        for attempt_id in ids:
            if self.cancel.is_set():
                break
            row = self.store.begin_upload(attempt_id)
            try:
                receipt = client.upload_grade(self.as_attempt(row), row["reviewed_score"], row["reviewed_comment"])
                if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                    raise ValueError("服务端未确认成绩回读一致。")
                self.store.mark_uploaded(attempt_id, receipt)
                self.log(f"{row['student_id']}：最终成绩已上传并回读核验。")
            except GradeUploadBlockedError as exc:
                error = self.redact_error(exc)
                self.store.mark_upload_blocked_before_send(attempt_id, error)
                raise ValueError(
                    f"{row['student_id']} 未发送成绩，已停止批次；人工审核结果已保留。{error}"
                ) from None
            except Exception as exc:
                error = self.redact_error(exc)
                self.store.mark_upload_uncertain(attempt_id, error)
                raise ValueError(f"{row['student_id']} 上传结果待核验，已停止批次。请先到 BB 检查，防止重复写入。{error}") from None


def seed_demo(store: Store, root: Path):
    store.upsert_assignment("demo-homework", "示例作业 · 数据结构第 1 次作业（虚构数据）", 10)
    for index, (student_id, name) in enumerate([
        ("PB23000018", "演示学生甲"), ("PB24000036", "演示学生乙"),
        ("PB25000001", "演示学生丙"), ("PB25000262", "演示学生丁"),
        ("PB25000263", "演示学生戊"), ("PB22000001", "演示学生己"),
    ]):
        store.upsert_student(student_id, name, f"demo-user-{index}")
        if not is_in_scope(student_id):
            continue
        attempt_id = f"demo-attempt-{index}"
        store.upsert_attempt(attempt_id, student_id, "demo-homework", submitted_at="2026-09-27 09:00")
        if store.get_attempt(attempt_id).get("ai_score") is None:
            score = [9, 7.5, 8, 6.5][index]
            store.update_attempt(attempt_id, ocr_text="【演示 OCR 文本，非真实学生作业】\n\n1. 顺序查找的最坏时间复杂度为 O(n)。\n2. 二分查找需要有序数组，时间复杂度为 O(log n)。\n3. 链表插入只需要改变相邻节点的指针。", ai_score=score,
                ai_comment="基本概念正确，需补充边界条件与推导过程。", rationale="第 1 题 3/3；第 2 题 3/3；第 3 题需结合具体位置讨论查找开销。此记录仅为演示。",
                uncertainties=["演示数据，不用于实际教学评价"], status="graded",
                provenance={"demo": True, "rubric": {"max_score": 10}})
