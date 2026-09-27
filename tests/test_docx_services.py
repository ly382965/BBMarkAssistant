from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from PIL import Image

from bb_assistant.services import OcrClient, OcrError


@pytest.fixture
def docx(tmp_path):
    path = tmp_path / "answer.docx"
    path.write_bytes(b"package parsing tested separately")
    return path


def image(path, ordinal=1):
    Image.new("RGB", (10, 10), (ordinal, 255, 0)).save(path)
    return SimpleNamespace(path=path, source_part="word/document.xml", relationship_id="rId1",
                           ordinal=ordinal, references=(("word/document.xml", "rId1", ordinal),))


@pytest.mark.parametrize("mode,method", [("command", "_command"), ("http", "_http"), ("mineru_v1", "_v1")])
def test_docx_preserves_math_table_and_ocr_images_for_every_provider(docx, tmp_path, mode, method):
    picture = image(tmp_path / "picture.png")
    paths = []

    def provider(self, path, *args):
        paths.append(path)
        if path.suffix == ".docx":
            return r"原生正文 $x^{2}+\frac{1}{2}$" + "\n| 答案 | 42 |\n![](data:image/png;base64,AAAA)"
        assert self.api_key == "fake-test-key"
        return "图片上的答案 IMAGE-7429"

    client = OcrClient({"mode": mode}, "fake-test-key")
    with patch("bb_assistant.services.extract_docx_images", return_value=[picture]), patch.object(OcrClient, method, provider):
        text = client.extract(docx, tmp_path / "out")
    assert paths == [docx, picture.path]
    assert "IMAGE-7429" in text and r"\frac{1}{2}" in text and "| 答案 | 42 |" in text
    assert "base64" not in text
    assert client.last_metadata["input_sha256"] != client.last_metadata["docx"]["images"][0]["ocr"]["input_sha256"]
    assert client.last_metadata["docx"]["embedded_images"] == 1
    assert Path(client.last_metadata["text_path"]).read_text(encoding="utf-8") == text


def test_image_only_docx_success_requires_recognized_text(docx, tmp_path):
    picture = image(tmp_path / "picture.png")
    with patch("bb_assistant.services.extract_docx_images", return_value=[picture]), \
            patch.object(OcrClient, "_command", side_effect=["![](data:image/png;base64,AAAA)", "扫描答案"]):
        assert "扫描答案" in OcrClient({}).extract(docx, tmp_path / "out")


@pytest.mark.parametrize("empty_result", ["![](images/no_text.jpg)", '<img src="data:image/png;base64,AAAA">', "![scan][image]\n[image]: data:image/png;base64,AAAA", "<table><tr><td></td></tr></table>"])
def test_image_link_is_not_a_successful_docx_ocr(docx, tmp_path, empty_result):
    picture = image(tmp_path / "picture.png")
    with patch("bb_assistant.services.extract_docx_images", return_value=[picture]), \
            patch.object(OcrClient, "_command", side_effect=["![](data:image/png;base64,AAAA)", empty_result]), \
            pytest.raises(OcrError, match="图片 1 识别失败"):
        OcrClient({}).extract(docx, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*/extracted.md"))


def test_any_embedded_image_failure_blocks_entire_docx(docx, tmp_path):
    pictures = [image(tmp_path / "one.png"), image(tmp_path / "two.png", 2)]
    with patch("bb_assistant.services.extract_docx_images", return_value=pictures), \
            patch.object(OcrClient, "_command", side_effect=["正文", "第一张答案", OcrError("模型失败")]), \
            pytest.raises(OcrError, match="图片 2 识别失败"):
        OcrClient({}).extract(docx, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*/extracted.md"))


def test_docx_can_stop_between_embedded_images(docx, tmp_path):
    pictures = [image(tmp_path / "one.png"), image(tmp_path / "two.png", 2)]
    client = OcrClient({})
    client.cancel_requested = Mock(side_effect=[False, True])
    progress = []
    client.progress = progress.append
    with patch("bb_assistant.services.extract_docx_images", return_value=pictures), \
            patch.object(OcrClient, "_command", side_effect=["正文", "第一张答案"]) as command, \
            pytest.raises(OcrError, match="已停止 DOCX"):
        client.extract(docx, tmp_path / "out")
    assert command.call_count == 2
    assert any("1/2" in line for line in progress)
    assert not list((tmp_path / "out").glob("*/extracted.md"))


def test_invalid_docx_never_reaches_provider(docx, tmp_path):
    with patch("bb_assistant.services.extract_docx_images", side_effect=ValueError("DOCX 文件损坏")), \
            patch.object(OcrClient, "_command") as provider, pytest.raises(OcrError, match="损坏"):
        OcrClient({}).extract(docx, tmp_path / "out")
    provider.assert_not_called()


def test_multiframe_docx_image_is_not_truncated_to_first_frame(docx, tmp_path):
    picture = image(tmp_path / "animated.gif")
    first = Image.new("RGB", (10, 10), "red")
    first.save(picture.path, save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
    with patch("bb_assistant.services.extract_docx_images", return_value=[picture]), \
            patch.object(OcrClient, "_command") as provider, pytest.raises(OcrError, match="多帧"):
        OcrClient({}).extract(docx, tmp_path / "out")
    provider.assert_not_called()
    with patch.object(OcrClient, "_command") as provider, pytest.raises(OcrError, match="多帧"):
        OcrClient({}).extract(picture.path, tmp_path / "direct-image")
    provider.assert_not_called()


def test_broken_docx_image_never_reaches_provider(docx, tmp_path):
    picture = image(tmp_path / "broken.png")
    picture.path.write_bytes(b"not an image")
    with patch("bb_assistant.services.extract_docx_images", return_value=[picture]), \
            patch.object(OcrClient, "_command") as provider, pytest.raises(OcrError, match="图片损坏"):
        OcrClient({}).extract(docx, tmp_path / "out")
    provider.assert_not_called()


def test_empty_docx_never_becomes_a_success(docx, tmp_path):
    with patch("bb_assistant.services.extract_docx_images", return_value=[]), \
            patch.object(OcrClient, "_command", return_value="  "), pytest.raises(OcrError, match="没有可识别"):
        OcrClient({}).extract(docx, tmp_path / "out")


@pytest.mark.parametrize("already_in_native", [False, True])
def test_legacy_equation_is_preserved_once(docx, tmp_path, already_in_native):
    latex = r"\frac{1}{2}+x^{2}"
    def inspect(path, output, **kwargs):
        kwargs["equations"].append(SimpleNamespace(latex=latex, source_part="word/document.xml", relationship_id="rIdFormula"))
        return []
    native = "学生正文" + ("\n$$\n" + latex + "\n$$" if already_in_native else "")
    client = OcrClient({})
    with patch("bb_assistant.services.extract_docx_images", side_effect=inspect), \
            patch.object(OcrClient, "_command", return_value=native):
        text = client.extract(docx, tmp_path / "out")
    assert text.count(latex) == 1
    assert client.last_metadata["docx"]["legacy_equations"][0]["already_in_native_text"] == already_in_native


@pytest.mark.parametrize("native,formula", [
    ("原生结果 $x=10$", "x=1"),
    (r"原生结果 $\text{a b}$", r"\text{ab}"),
    (r"原生结果 $\text{ab}$", r"\text{a b}"),
    ("正文中出现 x=1", "x=1"),
    (r"转义货币 \$x=1\$", "x=1"),
    ("不完整公式 $x=1", "x=1"),
])
def test_formula_is_not_dropped_for_prefix_spaces_or_uncertain_span(docx, tmp_path, native, formula):
    def inspect(path, output, **kwargs):
        kwargs["equations"].append(SimpleNamespace(
            latex=formula, source_part="word/document.xml", relationship_id="rIdFormula"
        ))
        return []

    client = OcrClient({})
    with patch("bb_assistant.services.extract_docx_images", side_effect=inspect), \
            patch.object(OcrClient, "_command", return_value=native):
        text = client.extract(docx, tmp_path / "out")
    assert f"$$\n{formula}\n$$" in text
    assert client.last_metadata["docx"]["legacy_equations"][0]["already_in_native_text"] is False


@pytest.mark.parametrize("native", ["$ x=1 $", "$$\nx=1\n$$", r"\(x=1\)", r"\[x=1\]"])
def test_exact_math_span_is_consumed_once_for_repeated_formulas(docx, tmp_path, native):
    def inspect(path, output, **kwargs):
        kwargs["equations"].extend(
            SimpleNamespace(latex="x=1", source_part="word/document.xml", relationship_id=f"formula{index}")
            for index in range(2)
        )
        return []

    client = OcrClient({})
    with patch("bb_assistant.services.extract_docx_images", side_effect=inspect), \
            patch.object(OcrClient, "_command", return_value=native):
        text = client.extract(docx, tmp_path / "out")
    assert text.count("x=1") == 2
    assert [entry["already_in_native_text"] for entry in client.last_metadata["docx"]["legacy_equations"]] == [True, False]


def test_multiple_exact_native_occurrences_preserve_recovered_count(docx, tmp_path):
    def inspect(path, output, **kwargs):
        kwargs["equations"].extend(
            SimpleNamespace(latex="x=1", source_part="word/document.xml", relationship_id=f"formula{index}")
            for index in range(3)
        )
        return []

    client = OcrClient({})
    with patch("bb_assistant.services.extract_docx_images", side_effect=inspect), \
            patch.object(OcrClient, "_command", return_value=r"$x=1$ 第一题；\[x=1\] 第二题"):
        text = client.extract(docx, tmp_path / "out")
    assert text.count("x=1") == 3
    assert [entry["already_in_native_text"] for entry in client.last_metadata["docx"]["legacy_equations"]] == [True, True, False]


def test_docx_v1_overrides_pdf_tier_without_mutating_image_config(docx, tmp_path):
    def response(data=None, content=b""):
        result = Mock(spec=requests.Response)
        result.status_code = 200
        result.json.return_value = data
        result.content = content
        result.headers = {}
        return result
    replies = [response({"id": "up1", "status": "completed", "file": {"id": "file1"}}),
               response({"job_id": "job1", "status": "completed", "files": [{"status": "completed", "output_files": {"markdown": {"file_id": "md1"}}}]}),
               response(content="DOCX正文".encode())]
    config = {"mode": "mineru_v1", "endpoint": "https://ocr.example", "tier": "standard", "extra_params": {"tier": "standard"}}
    client = OcrClient(config)
    with patch("bb_assistant.services.extract_docx_images", return_value=[]), \
            patch("bb_assistant.services.requests.request", side_effect=replies) as request:
        assert client.extract(docx, tmp_path / "out") == "DOCX正文"
    assert request.call_args_list[1].kwargs["json"]["tier"] == "flash"
    assert config["tier"] == "standard" and config["extra_params"]["tier"] == "standard"
