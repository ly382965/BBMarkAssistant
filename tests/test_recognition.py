"""Routing and cache tests use synthetic documents only; no remote calls."""

import io
import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from bb_assistant import recognition
from bb_assistant.recognition import prepare_submission
from bb_assistant.services import OcrClient, OcrError


class Classifier:
    def __init__(self, kind="printed", *, model="test-vision"):
        self.kind = kind
        self.config = {"model": model, "api_key": "test-secret-never-save"}
        self.calls = []

    def classify_images(self, images):
        self.calls.append(list(images))
        return [{"kind": self.kind, "reason": "synthetic"} for _ in images]


class Ocr:
    def __init__(self):
        self.config = {"mode": "test", "api_key": "test-secret-never-save"}
        self.calls = []

    def extract(self, path, output_dir):
        self.calls.append((path, output_dir))
        return "native text" if path.suffix in {".txt", ".md"} else "printed OCR text"


@pytest.fixture
def clients():
    return {"classifier": Classifier(), "ocr": Ocr()}


def raster(tmp_path, name="answer.png", color="white"):
    path = tmp_path / name
    Image.new("RGB", (160, 240), color).save(path)
    return path


def pdf(tmp_path, pages=2):
    path = tmp_path / "answer.pdf"
    images = [Image.new("RGB", (160, 240), (255, 255 - index, 250)) for index in range(pages)]
    images[0].save(path, save_all=True, append_images=images[1:])
    return path


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def docx(tmp_path, *, body="<w:p><w:r><w:t>学生正文</w:t></w:r></w:p>", image=False,
         extra=None, rels=""):
    path = tmp_path / "answer.docx"
    if image:
        body += '<w:p><w:r><w:drawing><a:blip r:embed="pic"/></w:drawing></w:r></w:p>'
        rels += f'<Relationship Id="pic" Target="media/pic.png" Type="{R}/image"/>'
    types = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Override PartName="/word/document.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '<Default Extension="png" ContentType="image/png"/></Types>')
    parts = {"[Content_Types].xml": types,
             "word/document.xml": f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}" xmlns:m="{M}"><w:body>{body}</w:body></w:document>',
             "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
             **(extra or {})}
    if image:
        output = io.BytesIO()
        Image.new("RGB", (200, 140), "white").save(output, format="PNG")
        parts["word/media/pic.png"] = output.getvalue()
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return path


def test_printed_document_uses_original_ocr_and_cache(tmp_path, clients):
    path = pdf(tmp_path, 5)
    first = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert not first.images
    assert "printed OCR text" in first.text
    assert [len(batch) for batch in clients["classifier"].calls] == [4, 1]
    assert clients["ocr"].calls[0][0] == path
    second = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["classifier"].calls) == 2
    assert len(clients["ocr"].calls) == 1
    assert second.metadata["documents"][0]["ocr_cached"] is True
    assert second.metadata["documents"][0]["render_cached"] is True
    for manifest in (tmp_path / "out").rglob("*.json"):
        assert "test-secret-never-save" not in manifest.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["handwritten", "mixed", "uncertain"])
def test_nonprinted_attachment_keeps_all_original_pages(tmp_path, clients, kind):
    clients["classifier"].kind = kind
    result = prepare_submission([pdf(tmp_path, 3)], tmp_path / "out", {}, **clients)
    assert len(result.images) == 3
    assert result.metadata["documents"][0]["route"] == "vision"
    assert not clients["ocr"].calls
    if kind == "uncertain":
        assert result.metadata["warnings"]


def test_one_handwritten_page_routes_whole_pdf_to_vision(tmp_path, clients):
    clients["classifier"].classify_images = lambda images: [
        {"kind": "handwritten" if index == 1 else "printed", "reason": ""}
        for index, _ in enumerate(images)]
    result = prepare_submission([pdf(tmp_path, 3)], tmp_path / "out", {}, **clients)
    assert len(result.images) == 3
    assert not clients["ocr"].calls


@pytest.mark.parametrize("bad", [None, [], [{"kind": "invalid"}], {"kind": "printed"}])
def test_invalid_classification_never_omits_pages(tmp_path, clients, bad):
    clients["classifier"].classify_images = lambda images: bad
    result = prepare_submission([pdf(tmp_path, 2)], tmp_path / "out", {}, **clients)
    assert len(result.images) == 2
    assert result.metadata["warnings"]
    assert not clients["ocr"].calls


def test_classifier_failure_falls_back_visibly_without_echoing_secret(tmp_path, clients):
    def broken(images):
        raise RuntimeError("URL contains secret-test-api-key")
    clients["classifier"].classify_images = broken
    messages = []
    result = prepare_submission([raster(tmp_path)], tmp_path / "out", {}, progress=messages.append, **clients)
    assert len(result.images) == 1
    assert "RuntimeError" in result.metadata["warnings"][0]
    assert "secret-test-api-key" not in json.dumps(result.metadata)
    assert any("全部页面" in message for message in messages)


def test_classification_failure_is_retried_and_can_recover_ocr_route(tmp_path, clients):
    path = raster(tmp_path)
    original = clients["classifier"].classify_images
    clients["classifier"].classify_images = lambda _: (_ for _ in ()).throw(RuntimeError("unavailable"))
    failed = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert failed.images
    clients["classifier"].classify_images = original
    recovered = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert not recovered.images
    assert recovered.metadata["documents"][0]["route"] == "ocr"
    assert not recovered.metadata["warnings"]


def test_forced_failed_classification_does_not_restore_old_success_cache(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    original = clients["classifier"].classify_images
    clients["classifier"].classify_images = lambda _: (_ for _ in ()).throw(RuntimeError("unavailable"))
    prepare_submission([path], tmp_path / "out", {}, force=True, **clients)
    clients["classifier"].classify_images = original
    prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["classifier"].calls) == 2


def test_classifier_previews_are_smaller_than_full_grading_images(tmp_path, clients):
    path = tmp_path / "large.png"
    Image.new("RGB", (2000, 3000), "white").save(path)
    clients["classifier"].kind = "handwritten"
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    with Image.open(clients["classifier"].calls[0][0]) as preview:
        assert max(preview.size) == 1600
    with Image.open(result.images[0]) as full:
        assert max(full.size) == 2200
    assert clients["classifier"].calls[0][0] != result.images[0]


@pytest.mark.parametrize("kind", ["png", "docx", "txt", "tiff"])
def test_ocr_warnings_survive_cache_and_all_attachment_routes(tmp_path, clients, kind):
    if kind == "docx":
        path = docx(tmp_path, image=True)
    elif kind == "txt":
        path = tmp_path / "answer.txt"
        path.write_text("answer", encoding="utf-8")
    elif kind == "tiff":
        path = tmp_path / "answer.tiff"
        Image.new("RGB", (100, 100), "red").save(path, save_all=True, append_images=[Image.new("RGB", (100, 100), "blue")])
    else:
        path = raster(tmp_path)
    clients["ocr"].last_metadata = {"warnings": ["公式或图片区域需要复核"]}
    first = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert any("公式或图片区域" in warning for warning in first.metadata["warnings"])
    clients["ocr"].last_metadata = {}
    cached = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert cached.metadata["warnings"] == first.metadata["warnings"]
    assert cached.metadata["documents"][0]["ocr_cached"]


def test_model_change_reclassifies_without_repeating_printed_ocr(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    clients["classifier"].config["model"] = "other-model"
    second = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["classifier"].calls) == 2
    assert len(clients["ocr"].calls) == 1
    assert second.metadata["documents"][0]["ocr_cached"] is True


def test_new_handwriting_decision_does_not_leak_stale_ocr(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    clients["classifier"].config["model"] = "new-model"
    clients["classifier"].kind = "handwritten"
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(result.images) == 1
    assert "printed OCR text" not in result.text
    assert len(clients["ocr"].calls) == 1


def test_changed_source_bytes_and_ocr_settings_invalidate_appropriate_cache(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    raster(tmp_path, color="red")
    changed = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["ocr"].calls) == 2
    assert len(clients["classifier"].calls) == 2
    clients["ocr"].config["mode"] = "other-mode"
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["ocr"].calls) == 3
    assert len(clients["classifier"].calls) == 2
    assert result.metadata["documents"][0]["source_sha256"] == changed.metadata["documents"][0]["source_sha256"]


def test_force_renews_classification_render_and_ocr(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    result = prepare_submission([path], tmp_path / "out", {}, force=True, **clients)
    assert len(clients["ocr"].calls) == 2
    assert len(clients["classifier"].calls) == 2
    assert not result.metadata["documents"][0]["ocr_cached"]


def test_forced_ocr_does_not_render_or_classify(tmp_path, clients, monkeypatch):
    path = tmp_path / "answer.pdf"
    path.write_bytes(b"mocked OCR input")
    monkeypatch.setattr(recognition, "_render", lambda *args: pytest.fail("must not render"))
    result = prepare_submission([path], tmp_path / "out", {"mode": "ocr"}, **clients)
    assert "printed OCR text" in result.text
    assert not clients["classifier"].calls


def test_vision_mode_bypasses_ocr_and_classifier_but_preserves_text_attachment(tmp_path, clients):
    path = raster(tmp_path)
    text = tmp_path / "answer.md"
    text.write_text("native text", encoding="utf-8")
    result = prepare_submission([path, text], tmp_path / "out", {"mode": "vision"}, **clients)
    assert len(result.images) == 1
    assert "native text" in result.text
    assert not clients["classifier"].calls
    assert [entry[0] for entry in clients["ocr"].calls] == [text]


@pytest.mark.parametrize("suffix", ["pdf", "png"])
def test_corrupt_inputs_fail_before_classification(tmp_path, clients, suffix):
    path = tmp_path / f"broken.{suffix}"
    path.write_bytes(b"not a valid document")
    with pytest.raises(OcrError, match="损坏|无法完整"):
        prepare_submission([path], tmp_path / "out", {}, **clients)
    assert not clients["classifier"].calls
    assert not clients["ocr"].calls


def test_missing_attachment_aborts_without_partial_result(tmp_path, clients):
    with pytest.raises(OcrError, match="不存在"):
        prepare_submission([raster(tmp_path), tmp_path / "missing.pdf"], tmp_path / "out", {}, **clients)
    assert not (tmp_path / "out" / "last-prepared.json").exists()


@pytest.mark.parametrize("setting,value", [("max_pages", 1), ("max_images", 1)])
def test_page_and_image_caps_never_truncate(tmp_path, clients, setting, value):
    with pytest.raises(OcrError, match=setting):
        prepare_submission([pdf(tmp_path, 2)], tmp_path / "out", {setting: value}, **clients)
    assert not clients["classifier"].calls


def test_request_size_cap_applies_to_text_without_truncation(tmp_path, clients):
    path = tmp_path / "answer.txt"
    path.write_text("text", encoding="utf-8")
    clients["ocr"].extract = lambda *args: "答案" * 1000
    with pytest.raises(OcrError, match="max_request_bytes"):
        prepare_submission([path], tmp_path / "out", {"max_request_bytes": 1024}, **clients)


def test_classification_request_cap_fails_before_sending_images(tmp_path, clients):
    path = tmp_path / "noise.png"
    Image.effect_noise((256, 256), 80).save(path)
    with pytest.raises(OcrError, match="max_request_bytes"):
        prepare_submission([path], tmp_path / "out", {"max_request_bytes": 1024}, **clients)
    assert not clients["classifier"].calls


def test_cancellation_stops_before_provider_and_does_not_publish_partial_manifest(tmp_path, clients):
    with pytest.raises(OcrError, match="已停止"):
        prepare_submission([raster(tmp_path)], tmp_path / "out", {}, cancelled=lambda: True, **clients)
    assert not clients["classifier"].calls
    assert not clients["ocr"].calls
    assert not (tmp_path / "out" / "last-prepared.json").exists()


def test_cache_rejects_modified_image_and_rebuilds_from_source(tmp_path, clients):
    clients["classifier"].kind = "handwritten"
    path = raster(tmp_path)
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    result.images[0].write_bytes(b"tampered")
    again = prepare_submission([path], tmp_path / "out", {}, **clients)
    with Image.open(again.images[0]) as restored:
        assert restored.size == (160, 240)
    assert not again.metadata["documents"][0]["render_cached"]


def test_cache_never_loads_text_outside_cache_root(tmp_path, clients):
    path = raster(tmp_path)
    prepare_submission([path], tmp_path / "out", {}, **clients)
    manifest = next((tmp_path / "out").glob("source-*/ocr-*.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    outside = tmp_path / "outside.txt"
    outside.write_text("WRONG PRIVATE CONTENT", encoding="utf-8")
    data["text"] = {"path": str(outside), "sha256": recognition._sha256(outside)}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert "WRONG PRIVATE CONTENT" not in result.text
    assert len(clients["ocr"].calls) == 2


def test_raster_all_tiff_pages_are_classified_and_ocred(tmp_path, clients):
    path = tmp_path / "pages.tiff"
    Image.new("RGB", (100, 100), "red").save(path, save_all=True, append_images=[Image.new("RGB", (100, 100), "blue")])
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["classifier"].calls[0]) == 2
    assert len(clients["ocr"].calls) == 2
    assert "第 1 页" in result.text and "第 2 页" in result.text


def test_animated_image_is_not_silently_reduced_to_first_frame(tmp_path, clients):
    path = tmp_path / "animated.gif"
    Image.new("RGB", (100, 100), "red").save(path, save_all=True, append_images=[Image.new("RGB", (100, 100), "blue")])
    with pytest.raises(OcrError, match="动画|多帧"):
        prepare_submission([path], tmp_path / "out", {}, **clients)


def test_docx_native_text_tables_deletions_and_omml_preserve_structure(tmp_path, clients):
    body = ('<w:p><w:del><w:r><w:delText>旧答案</w:delText></w:r></w:del>'
            '<w:ins><w:r><w:t>新答案</w:t></w:r></w:ins></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格答案</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<m:oMath><m:f><m:num><m:r><m:t>1</m:t></m:r></m:num>'
            '<m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:oMath>')
    result = prepare_submission([docx(tmp_path, body=body)], tmp_path / "out", {}, **clients)
    assert "旧答案" in result.text and "新答案" in result.text
    assert "已删除" in result.text and "插入" in result.text
    assert "表格答案" in result.text and "表格单元格" in result.text
    assert "OMML" in result.text and ":num>" in result.text and ":den>" in result.text
    assert result.metadata["documents"][0]["route"] == "native"
    assert not clients["ocr"].calls and not clients["classifier"].calls


def test_docx_handwritten_image_uses_native_text_and_all_images(tmp_path, clients):
    clients["classifier"].kind = "handwritten"
    path = docx(tmp_path, image=True)
    first = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(first.images) == 1
    assert "学生正文" in first.text and "内嵌图片 1" in first.text
    assert not clients["ocr"].calls
    again = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["classifier"].calls) == 1
    assert again.metadata["documents"][0]["classification_cached"]
    assert again.metadata["documents"][0]["render_cached"]
    assert again.images == first.images


def test_docx_printed_image_uses_whole_document_ocr(tmp_path, clients):
    path = docx(tmp_path, image=True)
    first = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert not first.images
    assert clients["ocr"].calls[0][0] == path
    clients["classifier"].config["model"] = "other"
    prepare_submission([path], tmp_path / "out", {}, **clients)
    assert len(clients["ocr"].calls) == 1


def test_docx_header_references_inside_paragraph_format_are_not_dropped(tmp_path, clients):
    body = '<w:p><w:pPr><w:sectPr><w:headerReference r:id="header"/></w:sectPr></w:pPr><w:r><w:t>主文档</w:t></w:r></w:p>'
    header = f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>页眉答案</w:t></w:r></w:p></w:hdr>'
    path = docx(tmp_path, body=body, rels=f'<Relationship Id="header" Target="header.xml" Type="{R}/header"/>', extra={"word/header.xml": header})
    result = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert "页眉答案" in result.text and "主文档" in result.text


def test_docx_decoded_legacy_equation_is_preserved_at_its_position(tmp_path, clients, monkeypatch):
    body = ('<w:p><w:r><w:t>前文</w:t></w:r><w:object>'
            '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" r:id="equation"/>'
            '</w:object><w:r><w:t>后文</w:t></w:r></w:p>')
    def inspect(path, output, *, equations, equation_decoder):
        equations.append(recognition.docx_package.DocxEquation(r"\frac{1}{2}", "word/document.xml", "equation"))
        return []
    # OLE validation/decoding is separately tested in test_docx_package.py.
    monkeypatch.setattr(recognition.docx_package, "extract_docx_images", inspect)
    result = prepare_submission([docx(tmp_path, body=body)], tmp_path / "out", {}, **clients)
    assert result.text.index("前文") < result.text.index(r"\frac{1}{2}") < result.text.index("后文")
    assert result.metadata["documents"][0]["legacy_equations"] == 1


def test_docx_font_dependent_symbol_cannot_silently_disappear(tmp_path, clients):
    body = '<w:p><w:r><w:t>答案</w:t><w:sym w:font="Wingdings" w:char="F0FC"/></w:r></w:p>'
    with pytest.raises(OcrError, match="特殊符号"):
        prepare_submission([docx(tmp_path, body=body)], tmp_path / "out", {}, **clients)


def test_docx_changed_cached_image_is_recovered_from_original_package(tmp_path, clients):
    clients["classifier"].kind = "handwritten"
    path = docx(tmp_path, image=True)
    first = prepare_submission([path], tmp_path / "out", {}, **clients)
    first.images[0].write_bytes(b"corrupt cache")
    second = prepare_submission([path], tmp_path / "out", {}, **clients)
    assert first.images != second.images
    with Image.open(second.images[0]) as image:
        assert image.size == (200, 140)


@pytest.mark.parametrize("body,rels", [
    ('<w:altChunk r:id="chunk"/>', f'<Relationship Id="chunk" Target="https://example.com/private" Type="{R}/aFChunk" TargetMode="External"/>'),
    ('<w:drawing><a:blip r:link="pic"/></w:drawing>', f'<Relationship Id="pic" Target="https://example.com/pic.png" Type="{R}/image" TargetMode="External"/>'),
    ('<w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" r:id="ole"/></w:object>', ""),
])
def test_unsupported_docx_content_is_never_silently_dropped(tmp_path, clients, body, rels):
    with pytest.raises(OcrError, match="外部|嵌入"):
        prepare_submission([docx(tmp_path, body=body, rels=rels)], tmp_path / "out", {}, **clients)
    assert not clients["classifier"].calls and not clients["ocr"].calls


def test_empty_docx_is_not_success(tmp_path, clients):
    with pytest.raises(OcrError, match="没有可识别"):
        prepare_submission([docx(tmp_path, body="")], tmp_path / "out", {}, **clients)


def test_source_change_during_recognition_aborts(tmp_path, clients):
    path = raster(tmp_path)
    def classify(images):
        raster(tmp_path, color="black")
        return [{"kind": "handwritten", "reason": ""} for _ in images]
    clients["classifier"].classify_images = classify
    with pytest.raises(OcrError, match="发生修改"):
        prepare_submission([path], tmp_path / "out", {}, **clients)
    assert not (tmp_path / "out" / "last-prepared.json").exists()


def test_real_local_ocr_subprocess_works_inside_appdata_depth(tmp_path, clients):
    # Exercise Windows CreateProcess cwd handling through the actual OCR client,
    # not just a string-length assertion or mocked subprocess.
    helper = tmp_path / "synthetic_ocr.py"
    helper.write_text(
        "import pathlib, sys\n"
        "output = pathlib.Path(sys.argv[2])\n"
        "(output / 'answer.md').write_text('printed answer preserved', encoding='utf-8')\n",
        encoding="utf-8",
    )
    root = tmp_path / "BBMarkAssistant" / "submissions" / ("a" * 24) / "recognition"
    client = OcrClient({"mode": "command", "command": [sys.executable, str(helper), "{input}", "{output}"]})
    result = prepare_submission([raster(tmp_path)], root, {}, classifier=clients["classifier"], ocr=client)
    assert "printed answer preserved" in result.text
    command_cwd = Path(client.last_metadata["output_dir"])
    assert (command_cwd / "answer.md").is_file()
    assert len(str(command_cwd)) < 260
    assert len(result.metadata["documents"][0]["source_sha256"]) == 64
