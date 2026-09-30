"""Prepare complete, source-linked grading inputs with cached handwriting routing.

Classification is advisory: an uncertain or unavailable classifier sends all pages
of that attachment to the visual grader. It can never justify omitting a page.
All caches are local, content addressed, and contain no service credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from zipfile import ZipFile

from defusedxml import ElementTree
from PIL import Image, ImageOps

from . import docx_package
from .local_equations import decode_legacy_equation
from .services import OcrError


CACHE_VERSION = 1
_RASTER = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_KINDS = {"printed", "handwritten", "mixed", "uncertain"}


@dataclass
class PreparedSubmission:
    text: str
    images: list[Path]
    metadata: dict


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _identity(client) -> str:
    # Store the digest only. Do not persist endpoint URLs, commands, headers,
    # secrets, or arbitrary provider metadata in recognition manifests.
    config = getattr(client, "config", {})
    if isinstance(config, dict):
        config = {key: value for key, value in config.items()
                  if not any(part in key.casefold() for part in ("secret", "password", "api_key", "header"))}
    return _digest({"client": f"{type(client).__module__}.{type(client).__qualname__}", "config": config})


def _integer(config: dict, key: str, default: int, low: int, high: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise OcrError(f"识别设置 {key} 必须是 {low} 到 {high} 的整数。")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OcrError(f"识别设置 {key} 必须是整数。") from exc
    if result != value or not low <= result <= high:
        raise OcrError(f"识别设置 {key} 必须是 {low} 到 {high} 的整数。")
    return result


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise OcrError("已停止作业识别；输入尚未完整准备，未生成分数。")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex[:12]}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("version") == CACHE_VERSION else None
    except (OSError, ValueError, UnicodeError):
        return None


def _cached_file(root: Path, record: dict) -> Path | None:
    try:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
            return None
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            return None
        if any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(root)):
            return None
        return path if _sha256(path) == record["sha256"] else None
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _file_record(path: Path, root: Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}


def _new_run(root: Path, prefix: str) -> Path:
    # Windows CreateProcess still rejects a long cwd in common installations,
    # even when Python itself can create/open those paths. The OCR client adds
    # another private run directory below this one, so keep each level short.
    result = root / f"{prefix}-{uuid.uuid4().hex[:12]}"
    result.mkdir(parents=True, exist_ok=False)
    return result


def _rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        result = Image.new("RGB", rgba.size, "white")
        result.paste(rgba, mask=rgba.getchannel("A"))
        return result
    return image.convert("RGB")


def _render_raster(path: Path, directory: Path, settings: dict, cancelled) -> list[Path]:
    results = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                frames = getattr(source, "n_frames", 1)
                if frames > settings["max_pages"]:
                    raise OcrError("图片页数超过 max_pages 限制；未截断作业。")
                if frames > 1 and source.format != "TIFF":
                    raise OcrError("作业图片包含动画或多帧；请导出为 PDF 或逐页图片，未只取第一帧。")
                for index in range(frames):
                    _check_cancelled(cancelled)
                    source.seek(index)
                    if min(source.size) <= 0 or source.width * source.height > settings["max_source_pixels"]:
                        raise OcrError("原始图片像素数超过 max_source_pixels 限制；请拆分或缩小原件。")
                    oriented = ImageOps.exif_transpose(source)
                    image = _rgb(oriented)
                    image.thumbnail((settings["max_page_edge"], settings["max_page_edge"]), Image.Resampling.LANCZOS)
                    output = directory / f"page-{index + 1:04d}.png"
                    image.save(output, format="PNG")
                    results.append(output)
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise OcrError("作业图片损坏或尺寸过大；无法完整读取原图。") from exc
    if not results:
        raise OcrError("作业图片没有可读取的页面。")
    return results


def _render_pdf(path: Path, directory: Path, settings: dict, cancelled) -> list[Path]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise OcrError("缺少 PDF 渲染组件 pypdfium2；请安装依赖或使用完整新版程序。") from exc
    results = []
    try:
        with pdfium.PdfDocument(str(path)) as document:
            if not 0 < len(document) <= settings["max_pages"]:
                raise OcrError("PDF 为空或超过 max_pages 页数限制；未截断作业。")
            for index in range(len(document)):
                _check_cancelled(cancelled)
                page = document[index]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                        raise OcrError(f"PDF 第 {index + 1} 页尺寸无效。")
                    scale = min(settings["render_dpi"] / 72, settings["max_page_edge"] / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        output = directory / f"page-{index + 1:04d}.png"
                        image = _rgb(bitmap.to_pil())
                        image.save(output, format="PNG")
                        results.append(output)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    except OcrError:
        raise
    except Exception as exc:
        # A rendering failure is never treated as a classifier disagreement.
        raise OcrError("PDF 无法完整渲染；文件可能损坏、加密或包含无法读取的页面，请导出为 PDF 后重试。") from exc
    return results


def _render(path: Path, root: Path, settings: dict, force: bool, cancelled) -> tuple[list[Path], bool]:
    key = _digest({"version": CACHE_VERSION, "source": _sha256(path), "render": settings})
    manifest = root / f"render-{key}.json"
    if not force and (cached := _read_json(manifest)):
        records = cached.get("images", [])
        if isinstance(records, list) and records:
            images = [_cached_file(root, record) for record in records if isinstance(record, dict)]
            if len(images) == len(records) and all(images) and len(images) <= settings["max_pages"]:
                return images, True
    directory = _new_run(root, "pages")
    images = (_render_pdf(path, directory, settings, cancelled) if path.suffix.lower() == ".pdf"
              else _render_raster(path, directory, settings, cancelled))
    _write_json(manifest, {"version": CACHE_VERSION, "images": [_file_record(image, root) for image in images]})
    return images, False


def _classify(images: list[Path], root: Path, classifier, settings: dict, force: bool,
              progress, cancelled) -> tuple[list[dict], list[str], bool]:
    key = _digest({"version": CACHE_VERSION, "classifier": _identity(classifier),
                   "images": [_sha256(image) for image in images],
                   "classifier_max_edge": settings["classifier_max_edge"],
                   "classifier_batch_size": settings["classifier_batch_size"]})
    manifest = root / f"classification-{key}.json"
    if force:
        manifest.unlink(missing_ok=True)
    if not force and (cached := _read_json(manifest)):
        values = cached.get("classification")
        if (isinstance(values, list) and len(values) == len(images)
                and all(isinstance(value, dict) and value.get("kind") in _KINDS
                        and isinstance(value.get("reason"), str) for value in values)):
            return values, list(cached.get("warnings", [])), True
    values, notices, failed = [], [], False
    preview_dir = _new_run(root, "classifier-previews")
    previews = []
    for index, path in enumerate(images, 1):
        _check_cancelled(cancelled)
        with Image.open(path) as original:
            preview = _rgb(original)
            preview.thumbnail((settings["classifier_max_edge"], settings["classifier_max_edge"]), Image.Resampling.LANCZOS)
            preview_path = preview_dir / f"preview-{index:04d}.png"
            preview.save(preview_path, format="PNG")
            previews.append(preview_path)
    for start in range(0, len(images), settings["classifier_batch_size"]):
        _check_cancelled(cancelled)
        batch = previews[start:start + settings["classifier_batch_size"]]
        if sum(4 * ((image.stat().st_size + 2) // 3) for image in batch) > settings["max_request_bytes"]:
            raise OcrError("手写分类图片编码大小超过 max_request_bytes 限制；请降低渲染尺寸，未截断图片。")
        progress(f"识别路由：正在检查第 {start + 1}–{start + len(batch)} / {len(images)} 页是否手写。")
        try:
            result = classifier.classify_images(batch)
            if not isinstance(result, list) or len(result) != len(batch):
                raise ValueError("分类页面数量不匹配")
            if any(not isinstance(item, dict) or item.get("kind") not in _KINDS
                   or not isinstance(item.get("reason", ""), str) for item in result):
                raise ValueError("分类结果格式无效")
            values.extend({"kind": item["kind"], "reason": item.get("reason", "")[:2000]} for item in result)
        except Exception as exc:
            _check_cancelled(cancelled)
            failed = True
            # Do not echo exceptions: provider errors can include credentialed URLs.
            notice = (f"第 {start + 1}–{start + len(batch)} 页手写分类未完成（{type(exc).__name__}）；"
                      "为避免遗漏答案，本附件全部页面改用原图评分。")
            notices.append(notice)
            progress(notice)
            values.extend({"kind": "uncertain", "reason": "分类失败，保守使用原图"} for _ in batch)
        _check_cancelled(cancelled)
    uncertain = [str(index + 1) for index, item in enumerate(values) if item["kind"] == "uncertain"]
    if uncertain and not notices:
        notice = f"第 {', '.join(uncertain)} 页无法确定是否手写；本附件全部页面使用原图评分。"
        notices.append(notice)
        progress(notice)
    # Service outages and malformed replies must be retried on the next normal
    # run; valid uncertainty is stable evidence and can be cached.
    if not failed:
        _write_json(manifest, {"version": CACHE_VERSION, "classification": values, "warnings": notices})
    return values, notices, False


def _ocr_warnings(client) -> list[str]:
    metadata = getattr(client, "last_metadata", {})
    values = metadata.get("warnings", []) if isinstance(metadata, dict) else []
    if isinstance(values, str):
        values = [values]
    key = getattr(client, "api_key", "")
    return [value.replace(key, "[REDACTED]") if isinstance(key, str) and key else value
            for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _ocr(path: Path, root: Path, client, force: bool, cancelled) -> tuple[str, bool, list[str]]:
    key = _digest({"version": CACHE_VERSION, "source": _sha256(path), "suffix": path.suffix.lower(),
                   "ocr": _identity(client)})
    manifest = root / f"ocr-{key}.json"
    if force:
        manifest.unlink(missing_ok=True)
    if not force and (cached := _read_json(manifest)):
        record = cached.get("text")
        if isinstance(record, dict) and (text_path := _cached_file(root, record)):
            try:
                text = text_path.read_text(encoding="utf-8")
                if text.strip():
                    notices = cached.get("warnings", [])
                    if isinstance(notices, list) and all(isinstance(value, str) for value in notices):
                        return text, True, notices
            except (OSError, UnicodeError):
                pass
    _check_cancelled(cancelled)
    directory = _new_run(root, "ocr")
    text = client.extract(path, directory)
    _check_cancelled(cancelled)
    if not isinstance(text, str) or not text.strip():
        raise OcrError("OCR 未返回完整可读正文；不能据此评分。")
    output = directory / "routing-text.txt"
    output.write_text(text, encoding="utf-8")
    notices = _ocr_warnings(client)
    _write_json(manifest, {"version": CACHE_VERSION, "text": _file_record(output, root), "warnings": notices})
    return text, False, notices


def _native_docx(path: Path, images, equations) -> str:
    """Retain text order, tables, edits, image positions, and full math structure.

    OMML is kept as XML instead of flattening fractions, matrices, or exponents.
    Native content is untrusted student input just like OCR output. Package
    validation has already rejected unsupported OLE/external/graphical content.
    """
    image_numbers = {(part, rid): index for index, image in enumerate(images, 1)
                     for part, rid, _ in image.references}
    formulas = {(item.source_part, item.relationship_id): item.latex for item in equations}
    pieces = []
    with ZipFile(path) as archive:
        pending, visited, numbering_used = ["word/document.xml"], set(), False
        while pending:
            part = pending.pop(0)
            if part in visited:
                continue
            visited.add(part)
            root = docx_package._xml(archive, part)
            relationships = docx_package._relationships(archive, part)
            # Section references can live under pPr, whose formatting is not
            # ordinary visible text. Traverse references before filtering it.
            for node in root.iter():
                namespace, name = docx_package._tag_parts(node.tag)
                if namespace not in docx_package._WORD_NS:
                    continue
                rid = docx_package._relationship_attribute(node, "id")
                if name in {"headerReference", "footerReference"}:
                    pending.append(docx_package._linked_part(part, rid, relationships, name.removesuffix("Reference")))
                elif name in {"footnoteReference", "endnoteReference"}:
                    kind = name.removesuffix("Reference") + "s"
                    for key, relation in relationships.items():
                        if relation.kind.endswith("/" + kind):
                            pending.append(docx_package._linked_part(part, key, relationships, kind))

            def convert(element) -> str:
                nonlocal numbering_used
                namespace, name = docx_package._tag_parts(element.tag)
                rid = docx_package._relationship_attribute(element, "id")
                if namespace in docx_package._MATH_NS and name in {"oMath", "oMathPara"}:
                    return "\n[原生数学公式 OMML；保留原始结构]\n" + ElementTree.tostring(element, encoding="unicode") + "\n"
                if name == "OLEObject":
                    formula = formulas.get((part, rid))
                    return f"\n$$\n{formula}\n$$\n" if formula else ""
                image_rid = (docx_package._relationship_attribute(element, "embed")
                             if name in {"blip", "svgBlip"} else rid if name == "imagedata" else "")
                if image_rid:
                    index = image_numbers.get((part, image_rid))
                    return f"\n[本附件内嵌图片 {index}，原始位置 {part}/{image_rid}]\n" if index else ""
                if namespace in docx_package._WORD_NS:
                    if name in {"t", "delText"}:
                        return element.text or ""
                    if name in {"br", "cr"}:
                        return "\n"
                    if name == "tab":
                        return "\t"
                    if name == "sym":
                        raise OcrError("DOCX 包含依赖字体编码的特殊符号；请导出为 PDF 以保留原始字形。")
                    if name == "numPr":
                        numbering_used = True
                        return "[段落自动编号 " + ElementTree.tostring(element, encoding="unicode") + "]"
                    if name == "instrText":
                        return "[Word 域指令：" + (element.text or "") + "]"
                    if name in {"pPr", "rPr"}:
                        return "".join(convert(child) for child in element if docx_package._tag_parts(child.tag)[1] == "numPr")
                if namespace in docx_package._DRAW_NS and name == "t":
                    return element.text or ""
                content = "".join(convert(child) for child in element)
                if namespace in docx_package._WORD_NS:
                    if name in {"del", "moveFrom"}:
                        return "[已删除/移出内容开始]" + content + "[已删除/移出内容结束]"
                    if name in {"ins", "moveTo"}:
                        return "[插入/移入内容开始]" + content + "[插入/移入内容结束]"
                    if name == "r":
                        strike = [node for node in element.iter() if docx_package._tag_parts(node.tag)[1] in {"strike", "dstrike"}]
                        if any(not any(key.endswith("}val") and value in {"0", "false", "off"}
                                       for key, value in node.attrib.items()) for node in strike):
                            content = "[划掉内容开始]" + content + "[划掉内容结束]"
                    if name in {"p", "tr"}:
                        return content + "\n"
                    if name == "tc":
                        return "[表格单元格]" + content
                    if name == "tbl":
                        return "\n[表格开始]\n" + content + "[表格结束]\n"
                return content

            content = convert(root).strip()
            if content:
                pieces.append(f"--- DOCX 部件 {part} ---\n{content}")
        if numbering_used:
            if "word/numbering.xml" not in archive.namelist():
                raise OcrError("DOCX 使用自动编号但缺少编号定义；请导出为 PDF 以保留题号。")
            numbering = docx_package._xml(archive, "word/numbering.xml")
            pieces.append("[上述自动编号的原始定义]\n" + ElementTree.tostring(numbering, encoding="unicode"))
    return "\n\n".join(pieces)


def _docx(path: Path, root: Path, settings: dict, classifier, ocr, mode: str,
          force: bool, progress, cancelled) -> tuple[str, list[Path], dict]:
    key = _digest({"version": CACHE_VERSION, "source": _sha256(path), "render": settings})
    manifest = root / f"docx-{key}.json"
    pictures, equations, images, prepared_cached = [], [], [], False
    if force:
        manifest.unlink(missing_ok=True)
    elif cached := _read_json(manifest):
        try:
            for value in cached["pictures"]:
                picture_path = _cached_file(root, value)
                if picture_path is None:
                    raise ValueError("missing cached picture")
                pictures.append(docx_package.DocxImage(
                    picture_path, value["source_part"], value["relationship_id"], value["ordinal"],
                    tuple(tuple(ref) for ref in value["references"]),
                ))
            for value in cached["images"]:
                image = _cached_file(root, value)
                if image is None:
                    raise ValueError("missing cached page")
                images.append(image)
            for value in cached["equations"]:
                equations.append(docx_package.DocxEquation(value["latex"], value["source_part"], value["relationship_id"]))
            if len(images) != len(pictures):
                raise ValueError("cached DOCX image coverage mismatch")
            prepared_cached = True
        except (KeyError, TypeError, ValueError):
            pictures, equations, images = [], [], []
    if not prepared_cached:
        directory = _new_run(root, "docx")
        try:
            pictures = docx_package.extract_docx_images(
                path, directory / "embedded", equations=equations,
                equation_decoder=lambda payload: decode_legacy_equation(payload, directory / "equations", 60),
            )
        except ValueError as exc:
            raise OcrError(str(exc)) from exc
        _check_cancelled(cancelled)
    if len(pictures) > settings["max_images"]:
        raise OcrError("DOCX 图片数量超过 max_images 限制；未省略任何附件图片。")
    # Include every unique referenced image, including equation fallback previews.
    if not prepared_cached:
        for index, picture in enumerate(pictures, 1):
            target = directory / f"image-{index:04d}"
            target.mkdir()
            pages = _render_raster(picture.path, target, settings, cancelled)
            if len(pages) != 1:
                raise OcrError("DOCX 内嵌图片包含多页，无法保持文档位置；请导出为 PDF。")
            images.extend(pages)
        _write_json(manifest, {"version": CACHE_VERSION,
            "pictures": [{**_file_record(picture.path, root), "source_part": picture.source_part,
                          "relationship_id": picture.relationship_id, "ordinal": picture.ordinal,
                          "references": picture.references} for picture in pictures],
            "images": [_file_record(image, root) for image in images],
            "equations": [{"latex": item.latex, "source_part": item.source_part,
                           "relationship_id": item.relationship_id} for item in equations]})
    metadata = {"warnings": [], "embedded_images": len(images), "legacy_equations": len(equations),
                "render_cached": prepared_cached,
                "image_references": [{"image": index, "references": [list(ref) for ref in image.references]}
                                     for index, image in enumerate(pictures, 1)]}
    if not images:
        text = _native_docx(path, pictures, equations)
        if not text.strip():
            raise OcrError("DOCX 没有可识别的正文或图片；请检查原件。")
        metadata["route"] = "native"
        return text, [], metadata
    if mode == "auto":
        classification, notices, cache_hit = _classify(images, root, classifier, settings, force, progress, cancelled)
        metadata.update({"classification": classification, "warnings": notices, "classification_cached": cache_hit})
        if all(item["kind"] == "printed" for item in classification):
            text, hit, notices = _ocr(path, root, ocr, force, cancelled)
            metadata.update({"route": "ocr", "ocr_cached": hit})
            metadata["warnings"].extend(notices)
            return text, [], metadata
    metadata["route"] = "vision"
    return _native_docx(path, pictures, equations), images, metadata


def prepare_submission(documents: list[Path], output_dir: Path, config: dict, *, classifier,
                       ocr, force: bool = False, progress=lambda message: None,
                       cancelled=lambda: False) -> PreparedSubmission:
    """Use OCR for printed attachments and complete original images for handwriting.

    ``mode`` is ``auto`` (default), ``ocr`` (explicit force), or ``vision``.
    Classification and OCR use independent caches, so changing GPT/DS models
    never discards valid OCR text for unchanged source bytes and OCR settings.
    Limits fail explicitly; pages/images/text are never silently truncated.
    """
    mode = config.get("mode", "auto")
    if mode not in {"auto", "ocr", "vision"}:
        raise OcrError("识别路由 mode 仅支持 auto、ocr 或 vision。")
    settings = {
        "render_dpi": _integer(config, "render_dpi", 200, 72, 600),
        "max_page_edge": _integer(config, "max_page_edge", 2200, 512, 6000),
        "classifier_max_edge": _integer(config, "classifier_max_edge", 1600, 512, 3000),
        "max_pages": _integer(config, "max_pages", 100, 1, 1000),
        "max_images": _integer(config, "max_images", 100, 1, 1000),
        "max_request_bytes": _integer(config, "max_request_bytes", 48 * 1024 * 1024, 1024, 512 * 1024 * 1024),
        "max_source_pixels": _integer(config, "max_source_pixels", 80_000_000, 1_000_000, 200_000_000),
        "max_file_bytes": _integer(config, "max_file_bytes", 128 * 1024 * 1024, 1024, 1024 * 1024 * 1024),
        "classifier_batch_size": _integer(config, "classifier_batch_size", 4, 1, 16),
    }
    if not documents:
        raise OcrError("没有可处理的作业附件。")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    text_parts, all_images, records, notices = [], [], [], []
    total_encoded = 0
    for ordinal, raw_path in enumerate(documents, 1):
        _check_cancelled(cancelled)
        path = Path(raw_path).resolve()
        if not path.is_file() or not path.stat().st_size:
            raise OcrError(f"作业附件不存在或为空：{path.name}。")
        if path.stat().st_size > settings["max_file_bytes"]:
            raise OcrError(f"附件 {path.name} 超过 max_file_bytes 限制；未截断内容。")
        suffix = path.suffix.lower()
        if suffix not in _RASTER | {".pdf", ".docx", ".txt", ".md"}:
            raise OcrError(f"不支持的作业附件类型：{suffix or '无后缀'}；请导出为 PDF、图片、DOCX 或文本。")
        digest = _sha256(path)
        file_root = root / f"source-{digest[:24]}-{suffix.removeprefix('.')}"
        file_root.mkdir(exist_ok=True)
        progress(f"识别路由：处理附件 {ordinal}/{len(documents)}：{path.name}。")
        record = {"name": path.name, "source_sha256": digest, "warnings": []}
        images, text = [], ""
        if mode == "ocr" or suffix in {".txt", ".md"}:
            text, hit, warnings_ = _ocr(path, file_root, ocr, force, cancelled)
            record.update({"route": "native" if suffix in {".txt", ".md"} else "ocr", "ocr_cached": hit})
            record["warnings"].extend(warnings_)
        elif suffix == ".docx":
            text, images, details = _docx(path, file_root, settings, classifier, ocr, mode, force, progress, cancelled)
            record.update(details)
        else:
            rendered, hit = _render(path, file_root, settings, force, cancelled)
            if len(rendered) > settings["max_images"]:
                raise OcrError("附件图片数量超过 max_images 限制；请拆分附件，未截断页面。")
            record.update({"pages": len(rendered), "render_cached": hit})
            route = "vision"
            if mode == "auto":
                classification, warnings_, hit = _classify(rendered, file_root, classifier, settings,
                                                         force, progress, cancelled)
                record.update({"classification": classification, "classification_cached": hit, "warnings": warnings_})
                if all(item["kind"] == "printed" for item in classification):
                    route = "ocr"
            record["route"] = route
            if route == "vision":
                images = rendered
            elif suffix in _RASTER and len(rendered) > 1:
                # OCR providers often read only TIFF's first page: submit all frames explicitly.
                parts, hits = [], []
                for page, image in enumerate(rendered, 1):
                    value, hit, warnings_ = _ocr(image, file_root, ocr, force, cancelled)
                    parts.append(f"--- 图片第 {page} 页 ---\n{value}")
                    hits.append(hit)
                    record["warnings"].extend(f"第 {page} 页：{warning}" for warning in warnings_)
                text = "\n\n".join(parts)
                record["ocr_cached"] = all(hits)
            else:
                text, hit, warnings_ = _ocr(path, file_root, ocr, force, cancelled)
                record["ocr_cached"] = hit
                record["warnings"].extend(warnings_)
        _check_cancelled(cancelled)
        if _sha256(path) != digest:
            raise OcrError(f"附件 {path.name} 在识别过程中发生修改；请重新处理，未使用旧内容评分。")
        if len(all_images) + len(images) > settings["max_images"]:
            raise OcrError("本份作业原图总数超过 max_images 限制；请拆分附件，未截断页面。")
        total_encoded += sum(4 * ((image.stat().st_size + 2) // 3) for image in images) + len(text.encode("utf-8"))
        if total_encoded > settings["max_request_bytes"]:
            raise OcrError("本份作业文字及原图的编码大小超过 max_request_bytes 限制；请降低渲染尺寸或拆分，未截断内容。")
        first = len(all_images) + 1
        record["image_numbers"] = list(range(first, first + len(images)))
        record["images"] = [{"number": index, **_file_record(image, root)}
                            for index, image in enumerate(images, first)]
        introduction = f"--- 附件 {ordinal}：{path.name} ---"
        if images:
            introduction += f"\n[本附件原图对应请求图片 {first}–{first + len(images) - 1}，请按顺序检查全部图片。]"
        text_parts.append(introduction + ("\n" + text if text else ""))
        all_images.extend(images)
        records.append(record)
        notices.extend(f"{path.name}：{warning}" for warning in record["warnings"])
        progress(f"{path.name}：" + {"ocr": "非手写，使用 OCR 正文", "native": "直接读取原生文字", "vision": "使用全部原图评分"}[record["route"]]
                 + ("（已复用缓存）" if record.get("ocr_cached") else "。"))
    metadata = {"version": CACHE_VERSION, "mode": mode, "documents": records, "warnings": notices,
                "image_count": len(all_images), "estimated_input_bytes": total_encoded}
    _write_json(root / "last-prepared.json", metadata)
    return PreparedSubmission("\n\n".join(text_parts), all_images, metadata)
