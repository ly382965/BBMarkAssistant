"""Reject contradictory counted grades; no network, credentials or coursework."""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from bb_assistant.services import GradingClient, GradingError


POLICY = {"mode": "error_count", "free_errors": 1, "deduction_per_error": 0.5, "unit": "subquestion"}


def assessment(question_id, verdict, reason):
    return {"question_id": question_id, "verdict": verdict, "reason": reason}


def run_grade(**fields):
    response = Mock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"comment": "", "uncertainties": [], **fields}, ensure_ascii=False),
    }}]}
    client = GradingClient({})
    with patch("bb_assistant.services.requests.request", return_value=response) as request:
        result = client.grade("合成答案", "允许轻微语法问题，核心逻辑必须正确。", "合成参考", 10,
                              scoring_policy=POLICY)
    return result, request.call_args.kwargs["json"]["messages"][0]["content"]


def test_all_question_verdicts_separate_facts_from_counted_errors():
    items = [assessment(str(i), "correct", "答案与要求一致。") for i in range(1, 5)]
    items += [assessment("5", "basically_correct", "仅漏写分号，教师允许。"),
              assessment("6", "wrong", "只处理了前缀，正负交错时无法分类所有元素。")]
    result, system = run_grade(question_assessments=items)
    assert result["score"] == 10
    assert result["provider_metadata"]["wrong_question_count"] == 1
    assert result["provider_metadata"]["question_assessments"] == items
    assert result["provider_metadata"]["wrong_questions"] == [{
        "question_id": "6", "reason": items[-1]["reason"],
    }]
    assert "逐题核查" in result["rationale"]
    assert "基本正确（按教师规则不计错）" in result["rationale"]
    assert "question_assessments" in system
    assert "不要输出 wrong_questions" in system
    assert result["provider_metadata"]["prompt_version"].endswith("error-count-v2")


@pytest.mark.parametrize("reason", [
    "学生答案为B，与标准答案B一致，正确。",
    "正确。",
    "本题基本正确。",
    "学生答案正确。",
    "核心逻辑基本正确：仅漏写分号。",
    "存在边界疑点，但整体核心逻辑正确。经核对，正确。",
])
@pytest.mark.parametrize("structured", [True, False])
def test_explicit_correct_conclusion_never_becomes_a_counted_wrong_answer(reason, structured):
    fields = {"question_assessments": [assessment("1", "wrong", reason)]} if structured else {
        "wrong_questions": [{"question_id": "1", "reason": reason}],
    }
    with pytest.raises(GradingError, match="矛盾.*未生成分数"):
        run_grade(**fields)


@pytest.mark.parametrize("reason", [
    "未正确处理正负交错的结点。",
    "答案不正确。",
    "正确答案应为B，学生选择了C。",
    "第一步正确；第二步删除了仍应保留的结点。",
    "选择了C，没有正确实现前驱更新。",
])
def test_correct_keyword_in_negative_or_intermediate_context_does_not_discard_error(reason):
    result, _ = run_grade(wrong_questions=[{"question_id": "1", "reason": reason}])
    assert result["provider_metadata"]["wrong_question_count"] == 1
    assert result["provider_metadata"]["wrong_questions"][0]["reason"] == reason


def test_uncertain_verdict_always_appears_in_review_questions():
    result, _ = run_grade(question_assessments=[assessment("2", "uncertain", "公式中分母无法辨认。")])
    assert result["provider_metadata"]["wrong_question_count"] == 0
    assert result["uncertainties"] == ["第 2 题待检查：公式中分母无法辨认。"]
    assert "待检查（未计入确认错题）" in result["rationale"]


@pytest.mark.parametrize("legacy", [[], [{"question_id": "2", "reason": "漏掉必要条件。"}]])
def test_dual_protocol_conflict_is_rejected(legacy):
    with pytest.raises(GradingError, match="不一致.*未生成分数"):
        run_grade(question_assessments=[assessment("1", "wrong", "漏掉必要条件。")], wrong_questions=legacy)


def test_dual_protocol_matching_wrong_ids_are_accepted():
    result, _ = run_grade(question_assessments=[assessment("1", "wrong", "漏掉必要条件。")],
                          wrong_questions=[{"question_id": "1", "reason": "学生没有检查必要条件。"}])
    assert result["provider_metadata"]["wrong_question_count"] == 1


def test_dual_protocol_does_not_hide_a_legacy_contradiction():
    with pytest.raises(GradingError, match="矛盾"):
        run_grade(question_assessments=[assessment("1", "wrong", "漏掉必要条件。")],
                  wrong_questions=[{"question_id": "1", "reason": "正确。"}])


@pytest.mark.parametrize("items", [
    [], None, {}, [assessment("1", "probably_wrong", "不能确定。")],
    [assessment("1", [], "不能确定。")],
    [assessment("1", "wrong", "")],
    [assessment("1", "correct", "符合题意。"), assessment("01", "wrong", "漏条件。")],
    [assessment("1", "correct", "符合题意。"), assessment("1.1", "wrong", "漏条件。")],
])
def test_invalid_or_overlapping_assessments_do_not_fall_back_to_legacy(items):
    with pytest.raises(GradingError):
        run_grade(question_assessments=items, wrong_questions=[])


def test_old_wrong_question_response_remains_compatible():
    result, _ = run_grade(wrong_questions=[{"question_id": str(i), "reason": "答案违反必要条件。"}
                                         for i in (1, 2, 3)])
    assert result["score"] == 9
    assert "question_assessments" not in result["provider_metadata"]
