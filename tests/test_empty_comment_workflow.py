"""An intentionally blank final comment must not regain the AI suggestion."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

from bb_assistant.blackboard import Assignment
from bb_assistant.settings import DEFAULTS
from bb_assistant.storage import Store
from bb_assistant.workflow import Workflow


def test_upload_preserves_intentionally_blank_reviewed_comment(tmp_path):
    store = Store(tmp_path)
    store.upsert_student("PB25000001", "测试学生")
    store.upsert_assignment("hw", "测试作业", 10)
    store.upsert_attempt("a1", "PB25000001", "hw")
    store.update_attempt("a1", ocr_text="已识别作业", status="ocr_done")
    store.update_attempt("a1", ai_score=8, ai_comment="AI 建议评语", status="graded")
    store.approve("a1", 9, "", 10)
    settings = SimpleNamespace(root=tmp_path, data=deepcopy(DEFAULTS), secret=lambda _: "")
    workflow = Workflow(settings, store)
    client = MagicMock()
    client.list_assignments.return_value = [Assignment("hw", "测试作业", 10)]
    client.upload_grade.return_value = {"verified": True, "score": 9, "feedback": ""}
    workflow.client = client

    workflow.upload(["a1"])

    client.upload_grade.assert_called_once()
    assert client.upload_grade.call_args.args[1:] == (9, "")
    row = store.get_attempt("a1")
    assert row["status"] == "uploaded"
    assert row["reviewed_comment"] == ""
    assert row["ai_comment"] == "AI 建议评语"
    assert row["receipt"]["feedback"] == ""
