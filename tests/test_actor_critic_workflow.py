"""Bounded collaboration, durable traces and unchanged human-review boundaries."""
import copy
import csv
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bb_assistant.actor_critic import ActorCriticGrader
from bb_assistant.services import GradingError
from bb_assistant.settings import DEFAULTS
from bb_assistant.storage import Store
from bb_assistant.workflow import Workflow


def draft(score=10, uncertainties=None):
    return {"score": score, "comment": "", "rationale": "逐题核对后的建议", "uncertainties": uncertainties or [],
            "provider_metadata": {"model_returned": "DS"}, "raw": "{}"}


def review(decision="accept"):
    return {"decision": decision, "summary": "原始证据复核结论", "question_assessments": [],
            "issues": [], "uncertainties": [], "suggested_score": 10,
            "provider_metadata": {"model_returned": "GPT"}, "raw": "{}"}


def clients():
    actor = MagicMock(config={"model": "DS"}, last_metadata={})
    critic = MagicMock(config={"model": "GPT"}, last_metadata={})
    actor.grade.return_value = draft()
    critic.critique.return_value = review()
    return actor, critic


def test_accept_stops_after_one_draft_and_one_review(tmp_path):
    actor, critic = clients()
    grader = ActorCriticGrader(actor, critic)
    pages = [tmp_path / "real-input.png"]
    result = grader.grade("source", "teacher rules", "answer", 10, images=pages)
    assert grader.trace["status"] == "accepted"
    assert grader.trace["revisions"] == 0
    assert [e["role"] for e in grader.trace["events"]] == ["actor", "critic"]
    assert actor.grade.call_args.kwargs["images"] == pages
    assert critic.critique.call_args.kwargs["images"] == pages
    assert critic.critique.call_args.kwargs["draft"] == actor.grade.return_value
    assert result["score"] == 10


def test_revision_is_rechecked_and_preserves_both_drafts():
    actor, critic = clients()
    actor.grade.side_effect = [draft(9.5), draft(10)]
    critic.critique.side_effect = [review("revise"), review("accept")]
    grader = ActorCriticGrader(actor, critic)
    result = grader.grade("source", "rules", "reference", 10)
    assert result["score"] == 10 and grader.trace["status"] == "accepted"
    assert [e["role"] for e in grader.trace["events"]] == ["actor", "critic", "actor", "critic"]
    assert grader.trace["events"][0]["result"]["score"] == 9.5
    assert actor.grade.call_args.kwargs["review_context"]["draft"]["score"] == 9.5
    assert critic.critique.call_args.kwargs["draft"]["score"] == 10


@pytest.mark.parametrize("maximum", [0, 1, 2])
def test_persistent_disagreement_stops_at_configured_bound(maximum):
    actor, critic = clients()
    critic.critique.return_value = review("revise")
    grader = ActorCriticGrader(actor, critic, {"max_revisions": maximum})
    result = grader.grade("source", "rules", "reference", 10)
    assert actor.grade.call_count == critic.critique.call_count == maximum + 1
    assert grader.trace["status"] == "needs_human"
    assert grader.trace["revisions"] == maximum
    assert result["uncertainties"]


def test_ambiguous_rules_do_not_trigger_a_forced_revision():
    actor, critic = clients()
    critic.critique.return_value = review("needs_human")
    grader = ActorCriticGrader(actor, critic)
    grader.grade("source", "rules", "reference", 10)
    assert actor.grade.call_count == 1 and grader.trace["status"] == "needs_human"


def test_actor_uncertainty_cannot_be_silently_accepted():
    actor, critic = clients()
    actor.grade.return_value = draft(10, ["需要核对一个符号"])
    grader = ActorCriticGrader(actor, critic)
    result = grader.grade("source", "rules", "reference", 10)
    assert grader.trace["status"] == "needs_human"
    assert "需要核对一个符号" in result["uncertainties"]


def test_critic_failure_retains_draft_without_returning_a_final_grade():
    actor, critic = clients()
    critic.critique.side_effect = GradingError("unavailable")
    grader = ActorCriticGrader(actor, critic)
    saved = []
    grader.on_trace = saved.append
    with pytest.raises(GradingError, match="unavailable"):
        grader.grade("source", "rules", "reference", 10)
    assert grader.trace["status"] == "incomplete"
    assert len(grader.trace["events"]) == 1
    assert saved[0]["events"][0]["result"]["score"] == 10


def test_cancel_between_draft_and_review_keeps_unreviewed_trace():
    actor, critic = clients()
    state = {"cancel": False}
    grader = ActorCriticGrader(actor, critic, cancelled=lambda: state["cancel"])
    grader.on_trace = lambda trace: state.update(cancel=True)
    with pytest.raises(GradingError, match="取消"):
        grader.grade("source", "rules", "reference", 10)
    critic.critique.assert_not_called()
    assert len(grader.trace["events"]) == 1


def test_changed_page_content_during_review_fails_closed():
    actor, critic = clients()
    actor.grade.return_value['provider_metadata']['image_sha256'] = ['original-page']
    critic.critique.return_value['provider_metadata']['image_sha256'] = ['different-page']
    grader = ActorCriticGrader(actor, critic)
    with pytest.raises(GradingError, match='发生变化'):
        grader.grade('source', 'rules', 'reference', 10)
    assert grader.trace['status'] == 'incomplete'
    assert len(grader.trace['events']) == 1


@pytest.fixture
def workflow_case(tmp_path, monkeypatch):
    config = copy.deepcopy(DEFAULTS)
    config['rubric'].update(instructions='教师规则', reference_answer='参考答案')
    settings = SimpleNamespace(root=tmp_path, data=config, secret=lambda name: name+'-test-key')
    store = Store(tmp_path)
    store.upsert_student('PB24000001', '单元测试学生')
    store.upsert_assignment('hw', '单元测试作业', 10)
    store.upsert_attempt('a1', 'PB24000001', 'hw', submitted_at='2026-09-29')
    source = tmp_path/'student.txt'
    source.write_text('待核对的原始作答', encoding='utf-8')
    store.update_attempt('a1', paths=[str(source)], status='downloaded')
    actor, critic = clients()
    monkeypatch.setattr('bb_assistant.workflow.GradingClient', lambda config,key: actor if key.startswith('deepseek') else critic)
    workflow = Workflow(settings, store)
    workflow.client = MagicMock()
    return SimpleNamespace(workflow=workflow,store=store,actor=actor,critic=critic,source=source,config=config)


def test_trace_is_durable_before_critic_call_and_independent_scores_survive(workflow_case, tmp_path):
    case=workflow_case
    case.workflow.process('hw','grade',provider='deepseek')
    previous=case.store.get_attempt('a1')['provenance']['grades']['deepseek']
    def inspect_trace(*args,**kwargs):
        row=case.store.get_attempt('a1')
        assert row['provenance']['actor_critic']['events'][0]['role']=='actor'
        assert row['ai_score'] is None
        return review()
    case.critic.critique.side_effect=inspect_trace
    assert case.workflow.process('hw','grade',provider='actor_critic')['completed']==1
    row=case.store.get_attempt('a1')
    assert row['provenance']['grades']['deepseek']==previous
    assert row['provenance']['active_grader']=='actor_critic'
    assert row['reviewed_score'] is None
    case.workflow.client.upload_grade.assert_not_called()
    output=case.store.export_report('hw',tmp_path/'results.csv')
    with output.open(encoding='utf-8-sig',newline='') as f:
        exported=next(csv.DictReader(f))
    assert exported['actor_critic_score']=='10'
    assert exported['actor_critic_status']=='accepted'


def test_failed_retry_cannot_reuse_previous_accepted_collaboration(workflow_case):
    case=workflow_case
    case.workflow.process('hw','grade',provider='deepseek')
    case.workflow.process('hw','grade',provider='actor_critic')
    case.critic.critique.side_effect=GradingError('gpt-test-key unavailable')
    summary=case.workflow.process('hw','grade',provider='actor_critic')
    row=case.store.get_attempt('a1')
    assert summary['failed']==1 and row['ai_score'] is None
    assert 'actor_critic' not in row['provenance']['grades']
    assert row['provenance']['actor_critic']['status']=='incomplete'
    assert 'gpt-test-key' not in row['error']
    assert 'gpt-test-key' not in row['provenance']['actor_critic']['error']
    case.workflow.select_grade('a1','deepseek')
    assert case.store.get_attempt('a1')['ai_score']==10


def test_reviewed_and_older_attempts_stay_protected(workflow_case):
    case=workflow_case
    case.workflow.process('hw','grade',provider='actor_critic')
    case.store.approve('a1',10,'',10)
    calls=case.actor.grade.call_count
    assert case.workflow.process('hw','grade',provider='actor_critic')['completed']==0
    assert case.actor.grade.call_count==calls
    with pytest.raises(ValueError,match='撤销'):
        case.workflow.select_grade('a1','actor_critic')
    case.store.upsert_attempt('a2','PB24000001','hw',submitted_at='2026-09-30')
    assert case.store.get_attempt('a1')['status']=='reviewed'


def test_preparation_failure_does_not_copy_trace_from_previous_student(workflow_case):
    case=workflow_case
    case.store.upsert_student('PB24000002','另一个单元测试学生')
    case.store.upsert_attempt('a2','PB24000002','hw',submitted_at='2026-09-29')
    case.store.update_attempt('a2',paths=[str(case.source.with_name('missing.txt'))],status='downloaded')
    summary=case.workflow.process('hw','grade',provider='actor_critic')
    assert summary['completed']==1 and summary['failed']==1
    assert not case.store.get_attempt('a2')['provenance'].get('actor_critic')


def test_ocr_coverage_warning_prevents_clean_collaboration_acceptance(workflow_case, monkeypatch):
    case=workflow_case
    monkeypatch.setattr('bb_assistant.recognition.prepare_submission', lambda *a,**k: SimpleNamespace(
        text='已识别部分正文',images=[],metadata={'documents':[], 'warnings':['有一页识别覆盖不足']},
    ))
    summary=case.workflow.process('hw','grade',provider='actor_critic')
    row=case.store.get_attempt('a1')
    assert summary['completed']==1
    assert row['provenance']['actor_critic']['status']=='needs_human'
    assert row['provenance']['grades']['actor_critic']['grading']['review_status']=='needs_human'
    assert '有一页识别覆盖不足' in row['uncertainties']
