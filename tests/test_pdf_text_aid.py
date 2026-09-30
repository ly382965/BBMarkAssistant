import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bb_assistant.services import OcrClient, OcrError


@pytest.fixture
def client(tmp_path):
    wrapper = tmp_path / "mineru_local.py"
    wrapper.write_text("# local fixture")
    wrapper.with_name("mineru_text_aid.py").write_text("# helper fixture")
    return OcrClient({"mode": "command", "command": ["python", str(wrapper)], "pdf_text_aid": True})


def test_supplement_is_additive_with_coverage_warnings(client, tmp_path):
    def run(args, **kwargs):
        assert kwargs["shell"] is False
        out = Path(args[-1])
        out.write_text("同一原件辅助转写：p=pa->next", encoding="utf-8")
        out.with_suffix(".warnings.json").write_text(json.dumps({"pages": [], "warnings": ["第2页需核对"]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch("bb_assistant.services.subprocess.run", side_effect=run):
        result = client._add_pdf_text_aid(tmp_path / "input.pdf", tmp_path, "主OCR")
    assert result.startswith("主OCR\n\n")
    assert "p=pa->next" in result
    assert client.last_metadata["warnings"] == ["第2页需核对"]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("fixture", 1), OSError("fixture")])
def test_failed_aid_preserves_primary_and_marks_manual_review(client, tmp_path, failure):
    with patch("bb_assistant.services.subprocess.run", side_effect=failure):
        result = client._add_pdf_text_aid(tmp_path / "input.pdf", tmp_path, "可靠主OCR")
    assert result == "可靠主OCR"
    assert client.last_metadata["pdf_text_aid"]["ok"] is False
    assert "不得将缺失内容当作未作答" in client.last_metadata["warnings"][0]


def test_aid_does_not_switch_a_custom_or_remote_provider(tmp_path):
    client = OcrClient({"mode": "http", "pdf_text_aid": True})
    with patch("bb_assistant.services.subprocess.run") as run:
        with pytest.raises(OcrError, match="本地 MinerU"):
            client._add_pdf_text_aid(tmp_path / "input.pdf", tmp_path, "主OCR")
    run.assert_not_called()


def test_binary_supplement_is_never_sent_to_grading(client, tmp_path):
    def run(args, **kwargs):
        out = Path(args[-1])
        out.write_text("![](data:image/png;base64,AAAA)", encoding="utf-8")
        out.with_suffix(".warnings.json").write_text('{"warnings": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch("bb_assistant.services.subprocess.run", side_effect=run):
        result = client._add_pdf_text_aid(tmp_path / "input.pdf", tmp_path, "主OCR")
    assert result == "主OCR"
    assert client.last_metadata["pdf_text_aid"]["ok"] is False


def test_disabled_local_preference_does_not_affect_api_provider(tmp_path):
    path = tmp_path / "answer.pdf"
    path.write_bytes(b"%PDF-fixture")
    client = OcrClient({"mode": "http", "pdf_text_aid": True})
    with patch.object(client, "_http", return_value="API 主 OCR"), patch.object(client, "_add_pdf_text_aid") as aid:
        assert client.extract(path, tmp_path / "out") == "API 主 OCR"
    aid.assert_not_called()
