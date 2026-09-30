"""PDF/image exports must never leak binary images into text-only grading."""

import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from bb_assistant.ocr_images import ImageDescriptionOnlyError, recover_images
from bb_assistant.services import OcrClient, OcrError


def picture(color="white"):
    stream = BytesIO()
    Image.new("RGB", (12, 12), color).save(stream, format="PNG")
    return stream.getvalue()


def data_uri(payload=None):
    return "data:image/png;base64," + base64.b64encode(payload or picture()).decode()


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "answer.pdf"
    path.write_bytes(b"%PDF-fixture")
    return path


@pytest.mark.parametrize("mode,method", [("command", "_command"), ("http", "_http"), ("mineru_v1", "_v1")])
def test_pdf_image_is_recognized_in_place_without_binary_payload(pdf, tmp_path, mode, method):
    original = f"第一题\n![]({data_uri()})\n第二题答案"
    client = OcrClient({"mode": mode})
    with patch.object(OcrClient, method, side_effect=[original, "第一题手写答案"]):
        result = client.extract(pdf, tmp_path / "out")
    assert "base64" not in result
    assert result.index("第一题手写答案") < result.index("第二题答案")
    assert client.last_metadata["output_images"]["supplemental_images"] == 1
    assert Path(client.last_metadata["text_path"]).read_text(encoding="utf-8") == result


def test_same_pdf_figure_is_processed_once_but_its_positions_are_preserved(pdf, tmp_path):
    original = f"题目\n![]({data_uri()})\n图注\n![]({data_uri()})"
    client = OcrClient({})
    with patch.object(OcrClient, "_command", side_effect=[original, "图内手写答案"]) as provider:
        result = client.extract(pdf, tmp_path / "out")
    assert provider.call_count == 2
    assert result.count("图内手写答案") == 1
    assert "图片 2 与图片 1 内容相同" in result
    assert client.last_metadata["output_images"]["images"][1]["same_as"] == 1


@pytest.mark.parametrize("kind", ["text_image", "flowchart", "natural_image"])
def test_advanced_details_preserved_without_repeating_ocr_and_warn_for_review(pdf, tmp_path, kind):
    original = f"正文\n![]({data_uri()})\n\n<details>\n<summary>{kind}</summary>\n\nP Q 图像内容\n</details>\n普通图注"
    client = OcrClient({})
    with patch.object(OcrClient, "_command", return_value=original) as provider:
        result = client.extract(pdf, tmp_path / "out")
    provider.assert_called_once()
    assert result.count("P Q 图像内容") == 1
    assert "普通图注" in result
    assert "base64" not in result
    assert client.last_metadata["warnings"]
    assert client.last_metadata["output_images"]["images"][0]["image_type"] == kind
    if kind == "natural_image":
        assert "仅供核对" in result
        assert "未识别答案" in client.last_metadata["warnings"][0]


def test_natural_image_description_alone_is_not_an_answer(pdf, tmp_path):
    original = f"![]({data_uri()})\n<details><summary>natural_image</summary>Anime drawing</details>"
    with patch.object(OcrClient, "_command", return_value=original), pytest.raises(OcrError, match="尚无可核验"):
        OcrClient({}).extract(pdf, tmp_path / "out")


@pytest.mark.parametrize("suffix", ["\n图 1：答案示意图", "\n<details><summary>caption</summary>答案示意图</details>", "\n<details><summary>text_image</summary> </details>"])
def test_plain_caption_or_empty_detail_is_not_proof_of_ocr(pdf, tmp_path, suffix):
    original = f"正文\n![]({data_uri()}){suffix}"
    with patch.object(OcrClient, "_command", side_effect=[original, "实际图内答案"]) as provider:
        assert "实际图内答案" in OcrClient({}).extract(pdf, tmp_path / "out")
    assert provider.call_count == 2


def test_second_uncovered_image_level_stops_without_unbounded_recursion(pdf, tmp_path):
    original = f"正文\n![]({data_uri()})"
    with patch.object(OcrClient, "_command", return_value=original) as provider, pytest.raises(OcrError, match="停止继续递归"):
        OcrClient({}).extract(pdf, tmp_path / "out")
    assert provider.call_count == 2
    assert not list((tmp_path / "out").glob("*/extracted.md"))


def test_supplemental_ocr_retains_child_warnings(pdf, tmp_path):
    original = f"正文\n![]({data_uri()})"
    child = f"额外文字\n![]({data_uri()})\n<details><summary>natural_image</summary>图像描述</details>"
    client = OcrClient({})
    with patch.object(OcrClient, "_command", side_effect=[original, child]):
        client.extract(pdf, tmp_path / "out")
    assert "图片 1 补识别" in client.last_metadata["warnings"][0]


def description_image(color="white"):
    return f"![]({data_uri(picture(color))})\n<details><summary>natural_image</summary>两条弯曲箭头</details>"


def test_description_only_child_preserves_parent_answer_and_runs_pdf_aid(pdf, tmp_path):
    original = f"第 5 题 while (p) p = p->next;\n![]({data_uri()})"
    client = OcrClient({"mode": "command", "pdf_text_aid": True,
                        "command": ["python", "mineru_local.py", "parse", "{input}"]})
    with patch.object(OcrClient, "_command", side_effect=[original, description_image()]), \
            patch.object(client, "_add_pdf_text_aid", side_effect=lambda path, out, text: text + "\n辅助正文") as aid:
        result = client.extract(pdf, tmp_path / "out")
    aid.assert_called_once()
    assert "while (p)" in result and "两条弯曲箭头" in result and "辅助正文" in result
    assert "base64" not in result
    record = client.last_metadata["output_images"]["images"][0]
    assert record["image_type"] == "natural_image"
    assert record["method"] == "supplemental_ocr"
    assert record["ocr"]["output_images"]["images"][0]["image_type"] == "natural_image"
    assert any("仅得到图像描述" in warning for warning in client.last_metadata["warnings"])
    assert Path(client.last_metadata["text_path"]).is_file()


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_description_only_child_cannot_make_whole_document_gradable(tmp_path, suffix):
    source = tmp_path / ("answer" + suffix)
    source.write_bytes(picture() if suffix == ".png" else b"%PDF-fixture")
    client = OcrClient({})
    with patch.object(OcrClient, "_command", side_effect=[f"![]({data_uri()})", description_image()]), \
            pytest.raises(OcrError, match="尚无可核验"):
        client.extract(source, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*/extracted.md"))
    assert client.last_metadata["output_images"]["images"][0]["image_type"] == "natural_image"


def test_description_child_does_not_skip_later_answer_image(pdf, tmp_path):
    original = f"正文\n![]({data_uri()})\n![]({data_uri(picture('red'))})"
    client = OcrClient({})
    with patch.object(OcrClient, "_command", side_effect=[original, description_image(), "第二图片中的真实作答"]) as provider:
        result = client.extract(pdf, tmp_path / "out")
    assert provider.call_count == 3
    assert "第二图片中的真实作答" in result
    assert client.last_metadata["output_images"]["supplemental_images"] == 2


@pytest.mark.parametrize("later", [OcrError("provider failed"), "正文 ![](../outside.png)",
                                  f"正文 ![]({data_uri(picture('blue'))})"])
def test_description_child_does_not_hide_later_failure_or_unsafe_image(pdf, tmp_path, later):
    original = f"正文\n![]({data_uri()})\n![]({data_uri(picture('red'))})"
    with patch.object(OcrClient, "_command", side_effect=[original, description_image(), later]), \
            pytest.raises(OcrError):
        OcrClient({}).extract(pdf, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*/extracted.md"))


def test_duplicate_description_images_do_not_count_as_answer(pdf, tmp_path):
    original = f"![]({data_uri()})\n![]({data_uri()})"
    with patch.object(OcrClient, "_command", side_effect=[original, description_image()]) as provider, \
            pytest.raises(OcrError, match="尚无可核验"):
        OcrClient({}).extract(pdf, tmp_path / "out")
    assert provider.call_count == 2


def test_description_only_state_contains_checked_metadata(tmp_path):
    with pytest.raises(ImageDescriptionOnlyError) as error:
        recover_images(description_image(), tmp_path, [tmp_path], lambda path, index: pytest.fail("no extra OCR"))
    assert "两条弯曲箭头" in error.value.text
    assert "base64" not in error.value.text
    assert error.value.records[0]["image_type"] == "natural_image"


def test_description_state_does_not_hide_uncovered_payload(tmp_path):
    with pytest.raises(ValueError) as error:
        recover_images(description_image() + "\ndata:image/png;base64,AAAA", tmp_path, [tmp_path],
                       lambda path, index: pytest.fail("no extra OCR"))
    assert not isinstance(error.value, ImageDescriptionOnlyError)


@pytest.mark.parametrize("form", ["![drawing]({})", '<img alt="drawing" src="{}">', "![drawing][figure]\n[figure]: {}", "![figure][]\n[figure]: {}"])
def test_supported_reference_forms_recover_image(tmp_path, form):
    text, records = recover_images(form.format(data_uri()), tmp_path, [tmp_path], lambda path, index: "图内答案")
    assert "base64" not in text
    assert "图内答案" in text
    assert len(records) == 1


def test_local_resource_in_markdown_subdirectory_is_read(tmp_path):
    nested = tmp_path / "native"
    nested.mkdir()
    (nested / "answer (1).png").write_bytes(picture())
    text, _ = recover_images("![](answer%20%281%29.png)", tmp_path, [tmp_path, nested], lambda path, index: "图片答案")
    assert "图片答案" in text


@pytest.mark.parametrize("source", [
    "https://example.invalid/figure.png", "//example.invalid/figure.png", "file:///C:/outside.png",
    "../outside.png", "%2e%2e/outside.png", "C:/outside.png", "\\\\server\\share\\image.png",
    "images/../../outside.png", "data:image/svg+xml;base64,AAAA", "data:image/png;base64,AAAA", "missing.png",
])
def test_unsafe_or_unreadable_assets_never_get_fetched_or_recognized(tmp_path, source):
    with patch("bb_assistant.services.requests.request") as request, pytest.raises(ValueError):
        recover_images(f"正文 ![]({source})", tmp_path, [tmp_path], lambda path, index: pytest.fail("must not OCR"))
    request.assert_not_called()


def test_image_count_is_bounded_before_decoding(tmp_path):
    original = "\n".join(f"![]({data_uri()})" for _ in range(65))
    with pytest.raises(ValueError, match="64"):
        recover_images(original, tmp_path, [tmp_path], lambda path, index: pytest.fail("must not OCR"))


def test_limits_reject_large_bytes_pixels_and_multiframe(tmp_path, monkeypatch):
    monkeypatch.setattr("bb_assistant.ocr_images.MAX_IMAGE_BYTES", 1)
    with pytest.raises(ValueError, match="大小限制"):
        recover_images(f"![]({data_uri()})", tmp_path, [tmp_path], lambda path, index: "unused")
    monkeypatch.setattr("bb_assistant.ocr_images.MAX_IMAGE_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr("bb_assistant.ocr_images.MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ValueError, match="像素"):
        recover_images(f"![]({data_uri()})", tmp_path, [tmp_path], lambda path, index: "unused")
    monkeypatch.setattr("bb_assistant.ocr_images.MAX_IMAGE_PIXELS", 40_000_000)
    animated = BytesIO()
    Image.new("RGB", (2, 2), "red").save(animated, format="GIF", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")])
    uri = "data:image/gif;base64," + base64.b64encode(animated.getvalue()).decode()
    with pytest.raises(ValueError, match="多帧"):
        recover_images(f"![]({uri})", tmp_path, [tmp_path], lambda path, index: "unused")


def test_cancel_before_supplemental_provider_call(pdf, tmp_path):
    client = OcrClient({})
    client.cancel_requested = lambda: True
    with patch.object(OcrClient, "_command", return_value=f"正文 ![]({data_uri()})") as provider, pytest.raises(OcrError, match="已停止"):
        client.extract(pdf, tmp_path / "out")
    provider.assert_called_once()


def test_html_missing_src_and_orphaned_data_never_become_answer(tmp_path):
    for original in ('<img alt="unknown">', "正文 data:image/png;base64,AAAA"):
        with pytest.raises(ValueError):
            recover_images(original, tmp_path, [tmp_path], lambda path, index: pytest.fail("must not OCR"))
