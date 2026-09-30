from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mineru_text_aid.py"
spec = importlib.util.spec_from_file_location("mineru_text_aid", SCRIPT)
aid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aid)


@pytest.mark.parametrize("size", [(595, 842), (1, 50000), (50000, 1), (100000, 100000)])
def test_render_limits_prevent_excessive_allocations(size):
    scale = aid.render_scale(*size)
    width, height = (math.ceil(value * scale) for value in size)
    assert max(width, height) <= aid.MAX_EDGE
    assert width * height <= aid.MAX_PIXELS
    assert scale <= 300 / 72


@pytest.mark.parametrize("size", [(0, 12), (-3, 12), (float("inf"), 12), (12, float("nan"))])
def test_invalid_geometry_fails_before_render(size):
    with pytest.raises(ValueError):
        aid.render_scale(*size)


@pytest.mark.parametrize("dpi", [0, 601, 300.5, True])
def test_invalid_dpi_rejected(dpi):
    with pytest.raises(ValueError):
        aid.render_scale(100, 100, dpi)


@pytest.mark.parametrize("pages", [0, 201, 1000])
def test_excess_pages_rejected_not_silently_truncated(pages):
    with pytest.raises(ValueError, match="未进行部分截取"):
        aid.validate_page_count(pages)


def test_boundary_page_counts_allowed():
    aid.validate_page_count(1)
    aid.validate_page_count(200)


@pytest.mark.parametrize("text,reason,expected", [
    (None, None, "empty_output"), ("   ", None, "empty_output"),
    ("x" * (aid.MAX_PAGE_CHARS + 1), None, "page_output_too_long"),
    ("int x = 1;", "length", "truncated_output"),
    ("x\n" * 30, None, "repetitive_output"),
    ("```cpp\nint x = 1;", None, "possibly_incomplete_output"),
])
def test_suspicious_transcription_is_explicit(text, reason, expected):
    assert expected in aid.assess_text(text, finish_reason=reason)


def test_image_only_layout_never_means_blank_submission():
    types = aid.layout_types([{"type": "image"}, {"type": "image_block"}])
    assert not aid.TEXT_TYPES.intersection(types)
    assert "不能据此断言" in aid.warning_message("no_text_candidates")
    assert aid.TEXT_TYPES.intersection(aid.layout_types([{"type": "code"}, {"type": "table"}]))


def test_metadata_keeps_page_numbers_and_content_is_not_corrected(tmp_path):
    text = "```cpp\nwhile (p != NULL) {\n  q = p->netx;  // model spelling retained\n}\n```"
    pages = [
        {"page": 1, "text": text, "status": "transcribed", "warnings": [], "chars": len(text)},
        {"page": 2, "status": "skipped_uncertain", "warnings": ["no_text_candidates"], "chars": 0},
        {"page": 3, "status": "error", "warnings": ["truncated_output"], "chars": 0},
    ]
    output = tmp_path / "aid.md"
    metadata = {"processing_complete": True, "requires_original_review": True}
    aid._write_outputs(output, pages, metadata)
    rendered = output.read_text(encoding="utf-8")
    assert text in rendered
    assert rendered.count(aid.NOTICE) == 4
    assert [rendered.index(f"原件第 {number} 页") for number in (1, 2, 3)] == sorted(
        rendered.index(f"原件第 {number} 页") for number in (1, 2, 3))
    assert "不是作业空白判定" in rendered
    meta = json.loads(output.with_suffix(".warnings.json").read_text(encoding="utf-8"))
    assert meta["transcribed_pages"] == 1
    assert [page["page"] for page in meta["pages"]] == [1, 2, 3]
    assert [warning["page"] for warning in meta["warnings"]] == [2, 3]
    assert all("text" not in page for page in meta["pages"])


def test_interrupted_output_declares_partial_coverage():
    assert "尚未完成" in aid.build_markdown([], processing_complete=False)


def test_invalid_document_writes_failure_metadata_without_loading_model(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("GPU must not be loaded")
    monkeypatch.setattr(aid, "_local_predictor", forbidden)
    source = tmp_path / "fake.pdf"
    source.write_bytes(b"not a PDF")
    output = tmp_path / "aid.md"
    with pytest.raises(ValueError, match="PDF"):
        aid.augment_pdf(source, output)
    metadata = json.loads(output.with_suffix(".warnings.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert not metadata["processing_complete"]
    assert metadata["document_error"]["code"] == "document_error"
