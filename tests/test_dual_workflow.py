"""Independent provider results, routing and the human-review boundary."""
import copy
import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from bb_assistant.settings import DEFAULTS
from bb_assistant.storage import Store
from bb_assistant.workflow import Workflow


@pytest.fixture
def example(tmp_path, monkeypatch):
    config = copy.deepcopy(DEFAULTS)
    config['recognition']['mode'] = 'auto'
    config['rubric'].update(instructions='按实际答案核对', reference_answer='测试参考', max_score=10)
    settings = SimpleNamespace(root=tmp_path, data=config, secret=lambda provider: provider + '-test-key')
    store = Store(tmp_path)
    store.upsert_student('PB24000001', '虚构测试学生')
    store.upsert_assignment('hw', '测试作业', 10)
    store.upsert_attempt('a1', 'PB24000001', 'hw', submitted_at='2026-09-29')
    image = tmp_path / 'source.png'
    Image.new('RGB', (120, 80), 'white').save(image)
    store.update_attempt('a1', paths=[str(image)], status='downloaded')
    workflow = Workflow(settings, store)
    workflow.client = MagicMock()
    providers = {}
    for name, score in [('gpt', 9.5), ('deepseek', 10)]:
        client = MagicMock()
        client.grade.return_value = dict(score=score, comment=name, rationale=name + ' evidence', uncertainties=[])
        client.last_metadata = {'model_returned': name}
        providers[name] = client
    monkeypatch.setattr('bb_assistant.workflow.GradingClient', lambda config, key: providers[key.split('-')[0]])
    prepare = MagicMock(return_value=SimpleNamespace(
        text='', images=[image], metadata={'documents': [{'name': image.name, 'route': 'vision'}], 'warnings': []},
    ))
    monkeypatch.setattr('bb_assistant.recognition.prepare_submission', prepare)
    ocr = MagicMock()
    monkeypatch.setattr('bb_assistant.workflow.OcrClient', ocr)
    return SimpleNamespace(config=config, settings=settings, store=store, workflow=workflow,
                           image=image, providers=providers, prepare=prepare, ocr=ocr)


def test_handwriting_bypasses_ocr_and_keeps_two_results(example):
    assert example.workflow.process('hw', 'grade', provider='gpt')['completed'] == 1
    first = example.store.get_attempt('a1')['provenance']['grades']['gpt']
    assert example.workflow.process('hw', 'grade', provider='deepseek')['completed'] == 1
    row = example.store.get_attempt('a1')
    assert row['provenance']['grades']['gpt'] == first
    assert row['provenance']['grades']['deepseek']['score'] == 10
    assert row['ai_score'] == 10 and row['provenance']['active_grader'] == 'deepseek'
    assert row['reviewed_score'] is None
    example.ocr.assert_not_called()
    for client in example.providers.values():
        assert client.grade.call_args.kwargs['images'] == [example.image]
        assert client.grade.call_args.args[0] == ''
    example.workflow.client.upload_grade.assert_not_called()


def test_choose_provider_and_reviewed_results_are_protected(example):
    example.workflow.process('hw', 'grade', provider='gpt')
    example.workflow.process('hw', 'grade', provider='deepseek')
    example.workflow.select_grade('a1', 'gpt')
    row = example.store.get_attempt('a1')
    assert row['ai_score'] == 9.5 and row['ai_comment'] == 'gpt'
    example.store.approve('a1', 9.75, '', 10)
    approved = example.store.get_attempt('a1')
    with pytest.raises(ValueError, match='撤销'):
        example.workflow.select_grade('a1', 'deepseek')
    for name in ('gpt', 'deepseek'):
        assert example.workflow.process('hw', 'grade', provider=name)['completed'] == 0
    assert example.store.get_attempt('a1') == approved


def test_changed_prompt_regrades_selected_provider_and_preserves_other(example):
    example.workflow.process('hw', 'grade', provider='deepseek')
    old = example.store.get_attempt('a1')['provenance']['grades']['deepseek']
    example.config['rubric']['instructions'] = '修正后的规则'
    for _ in range(2):
        example.workflow.process('hw', 'grade', provider='gpt')
    assert example.providers['gpt'].grade.call_count == 2
    assert example.providers['gpt'].grade.call_args.args[1] == '修正后的规则'
    assert example.store.get_attempt('a1')['provenance']['grades']['deepseek'] == old


def test_failed_rerun_removes_only_that_providers_stale_result(example):
    for name in ('gpt', 'deepseek'):
        example.workflow.process('hw', 'grade', provider=name)
    example.providers['gpt'].grade.side_effect = RuntimeError('gpt-test-key unavailable')
    summary = example.workflow.process('hw', 'grade', provider='gpt')
    assert summary['failed'] == 1
    row = example.store.get_attempt('a1')
    assert row['ai_score'] is None
    assert set(row['provenance']['grades']) == {'deepseek'}
    assert 'gpt-test-key' not in row['error']
    assert 'gpt-test-key' not in Path(summary['report_path']).read_text(encoding='utf-8')
    example.workflow.select_grade('a1', 'deepseek')
    assert example.store.get_attempt('a1')['ai_score'] == 10


def test_changed_attachment_invalidates_both_and_cannot_select_old(example):
    for name in ('gpt', 'deepseek'):
        example.workflow.process('hw', 'grade', provider=name)
    Image.new('RGB', (120, 80), 'black').save(example.image)
    with pytest.raises(ValueError, match='附件已变化'):
        example.workflow.select_grade('a1', 'gpt')
    example.workflow.process('hw', 'grade', provider='deepseek')
    assert set(example.store.get_attempt('a1')['provenance']['grades']) == {'deepseek'}


def test_ocr_cache_preparation_does_not_destroy_grades(example):
    example.workflow.process('hw', 'grade', provider='gpt')
    before = example.store.get_attempt('a1')
    summary = example.workflow.process('hw', 'ocr')
    assert summary['skipped_ocr'] == 1
    assert summary['completed'] == 0
    assert example.store.get_attempt('a1') == before
    example.workflow.process('hw', 'ocr', force_ocr=True)
    forced = example.store.get_attempt('a1')
    assert forced['ai_score'] is None and not forced['provenance'].get('grades')
    assert example.prepare.call_args.kwargs['force'] is True


def test_printed_route_sends_only_text_to_grader(example):
    example.prepare.return_value = SimpleNamespace(text='实际 OCR 文本', images=[], metadata={
        'documents': [{'name': 'source.png', 'route': 'ocr'}], 'warnings': ['OCR 需对照原文'],
    })
    example.workflow.process('hw', 'grade', provider='gpt')
    call = example.providers['gpt'].grade.call_args
    assert call.args[0] == '实际 OCR 文本'
    assert 'images' not in call.kwargs
    assert example.store.get_attempt('a1')['uncertainties'] == ['OCR 需对照原文']


def test_newer_attempt_never_uses_older_provider_result(example):
    example.workflow.process('hw', 'grade', provider='gpt')
    example.store.upsert_attempt('a2', 'PB24000001', 'hw', submitted_at='2026-09-30')
    with pytest.raises(ValueError, match='更新提交'):
        example.workflow.select_grade('a1', 'gpt')


def test_switching_from_vision_to_forced_ocr_rebuilds_input(example):
    example.workflow.process('hw', 'grade', provider='gpt')
    example.config['recognition']['mode'] = 'ocr'
    example.prepare.return_value = SimpleNamespace(text='真正的识别答案', images=[], metadata={
        'documents': [{'name': 'source.png', 'route': 'ocr'}], 'warnings': [],
    })
    example.workflow.process('hw', 'grade', provider='deepseek')
    assert example.prepare.call_args.args[2]['mode'] == 'ocr'
    assert example.providers['deepseek'].grade.call_args.args[0] == '真正的识别答案'
    assert 'images' not in example.providers['deepseek'].grade.call_args.kwargs


def test_missing_source_invalidates_selected_grade_before_error(example):
    example.workflow.process('hw', 'grade', provider='gpt')
    example.image.unlink()
    summary = example.workflow.process('hw', 'grade', provider='gpt')
    row = example.store.get_attempt('a1')
    assert summary['failed'] == 1 and row['ai_score'] is None
    assert 'gpt' not in row['provenance'].get('grades', {})


def test_forced_ocr_result_has_source_fingerprint(example):
    example.config['recognition']['mode'] = 'ocr'
    example.store.update_attempt('a1', ocr_text='已经识别的正文', status='ocr_done')
    example.workflow.process('hw', 'grade', provider='gpt')
    assert example.store.get_attempt('a1')['provenance']['grades']['gpt']['source_sha256']
    Image.new('RGB', (120, 80), 'green').save(example.image)
    with pytest.raises(ValueError, match='附件已变化'):
        example.workflow.select_grade('a1', 'gpt')


def test_csv_export_keeps_both_provider_scores_and_active_source(example, tmp_path):
    example.workflow.process('hw', 'grade', provider='gpt')
    example.workflow.process('hw', 'grade', provider='deepseek')
    example.workflow.select_grade('a1', 'gpt')
    report = example.store.export_report('hw', tmp_path / 'results.csv')
    with report.open(encoding='utf-8-sig', newline='') as handle:
        row = next(csv.DictReader(handle))
    assert row['gpt_score'] == '9.5' and row['ds_score'] == '10'
    assert row['active_grader'] == 'gpt' and row['ai_score'] == '9.5'
    assert row['reviewed_score'] == ''
