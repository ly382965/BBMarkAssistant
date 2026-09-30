"""Optional local, whole-page PDF transcription aid; never a replacement answer.

Run with the existing .mineru-venv Python: INPUT.pdf OUTPUT.md.
The adjacent OUTPUT.warnings.json records page coverage and every failure.
No inference dependencies are imported until augment_pdf() is called.
"""

from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any


MAX_PAGES = 200
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_EDGE = 3500
MAX_PIXELS = 8_000_000
MAX_PAGE_CHARS = 30_000
MAX_TOTAL_CHARS = 300_000
NOTICE = (
    "同一原件补充转写，不是新答案/修改答案，冲突需看原图，不可凭任一路缺失判未作答。"
    "本视图可能漏掉跨栏、图表或手写内容；必须结合主 OCR 和原件审核，不保证转写完整。"
)
TEXT_TYPES = frozenset({
    "text", "code", "algorithm", "equation", "equation_block", "formula_number",
    "list", "list_item", "table", "title", "doc_title", "paragraph_title",
    "aside_text", "ref_text", "index", "phonetic", "caption", "table_caption",
    "image_caption", "code_caption", "table_footnote", "image_footnote", "footnote",
    "header", "footer", "page_number", "page_footnote",
})


def render_scale(width: float, height: float, dpi: int = 300) -> float:
    """Bound allocation before rendering, including pathological PDF dimensions."""
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("PDF 页面尺寸无效。")
    if not isinstance(dpi, int) or isinstance(dpi, bool) or not 72 <= dpi <= 600:
        raise ValueError("DPI 必须为 72–600 的整数。")
    # Leave two pixels for PDFium's ceil rounding in each dimension.
    return min(dpi / 72, (MAX_EDGE - 2) / max(width, height),
               math.sqrt(MAX_PIXELS) / math.sqrt(width) / math.sqrt(height) * 0.999)


def validate_page_count(count: int) -> None:
    if not 1 <= count <= MAX_PAGES:
        raise ValueError(f"PDF 页数必须为 1–{MAX_PAGES}；实际 {count} 页，未进行部分截取。")


def layout_types(blocks: Any) -> list[str]:
    if blocks is None:
        return []
    return sorted({str(block.get("type", "unknown")).lower() for block in blocks})


def assess_text(text: str | None, *, finish_reason: str | None = None) -> list[str]:
    """Detect failures without rewriting, repairing, or completing any answer."""
    if finish_reason and finish_reason != "stop":
        return ["truncated_output" if finish_reason == "length" else "unexpected_finish"]
    if text is None or not text.strip():
        return ["empty_output"]
    if len(text) > MAX_PAGE_CHARS:
        return ["page_output_too_long"]
    warnings = []
    if len(text.strip()) < 12:
        warnings.append("very_short_output")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 20 and max(Counter(lines).values()) >= max(10, len(lines) // 2):
        warnings.append("repetitive_output")
    if "\ufffd" in text or text.count("```") % 2:
        warnings.append("possibly_incomplete_output")
    return warnings


def warning_message(code: str) -> str:
    return {
        "no_text_candidates": "布局仅识别到图片或未知块；不能据此断言无文字或未作答，请看原件。",
        "empty_layout": "布局识别为空，尚不能判断本页内容，请看原件。",
        "empty_output": "辅助转写为空，不能据此断言本页未作答。",
        "page_output_too_long": "本页输出异常超长，已拒绝采用，未静默截断。",
        "total_output_too_long": "辅助转写总长度超过限制，本页未采用，未静默截断。",
        "truncated_output": "模型输出被截断，本页不作为完整辅助转写。",
        "unexpected_finish": "模型未正常结束，本页不作为完整辅助转写。",
        "very_short_output": "辅助转写过短，可能漏识内容，请对照原件。",
        "repetitive_output": "辅助转写存在大量重复，可能识别异常，请对照原件。",
        "possibly_incomplete_output": "辅助转写含异常字符或未闭合代码块，请对照原件。",
        "page_error": "本页辅助识别出错，保留主 OCR 并对照原件。",
        "document_error": "文档辅助识别未完成，不能作为完整作业文本。",
    }.get(code, "本页辅助转写需要原件复核。")


def build_markdown(pages: list[dict[str, Any]], *, processing_complete: bool) -> str:
    parts = ["# 原件辅助转写", NOTICE]
    if not processing_complete:
        parts.append("**本次辅助识别尚未完成；以下结果可能只有部分页面。**")
    for page in pages:
        parts.extend([f"## 原件第 {page['page']} 页", NOTICE])
        for code in page.get("warnings", []):
            parts.append(f"> 待检查：{warning_message(code)}")
        if page.get("text"):
            # Keep model text byte-for-byte; do not fix spelling or code tokens.
            parts.append(page["text"])
        else:
            parts.append("[本页未取得可采用的辅助转写；不是作业空白判定。]")
    return "\n\n".join(parts) + "\n"


@lru_cache(maxsize=1)
def _local_predictor():
    try:
        from .mineru_local import prepare_environment
    except ImportError:
        from mineru_local import prepare_environment
    home = prepare_environment()
    from mineru.config import VlmConfig, config
    from mineru.model.registry import MINERU_2_5_PRO_2605_1_2B_GGUF
    from mineru.model.vlm.client import get_vlm_predictor

    repo = MINERU_2_5_PRO_2605_1_2B_GGUF
    expected_base = (home / "models").resolve()
    if config.model.source != "local" or Path(config.model.base_dir).resolve() != expected_base:
        raise RuntimeError("辅助识别只允许项目内已部署的本地模型；当前模型配置不符合要求。")
    if not all((expected_base / repo.name / item).is_file() for item in repo.paths.values()):
        raise FileNotFoundError("缺少已部署的本地 GGUF 模型；辅助识别不会下载新模型。")
    # Same cache key as the existing local parser: one GPU model for the process.
    predictor, engine = get_vlm_predictor(VlmConfig(engine="llama-cpp", server_url=""))
    if engine != "llama-cpp-engine":
        raise RuntimeError("辅助识别只允许本地 llama.cpp。")
    return predictor


def _write_outputs(output: Path, pages: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    metadata["pages"] = [{key: value for key, value in page.items() if key != "text"} for page in pages]
    metadata["warnings"] = [
        {"page": page["page"], "code": code, "message": warning_message(code)}
        for page in pages for code in page.get("warnings", [])
    ]
    metadata["transcribed_pages"] = sum(bool(page.get("text")) for page in pages)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_markdown(pages, processing_complete=metadata["processing_complete"]),
                      encoding="utf-8")
    output.with_suffix(".warnings.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def augment_pdf(path: str | Path, output: str | Path, *, dpi: int = 300) -> dict[str, Any]:
    """Write an optional auxiliary transcription with explicit per-page coverage.

    Calls in one process reuse the same existing local model. The caller must
    serialize inference with the main OCR pipeline. Fatal errors are recorded
    and raised; individual page failures are recorded and processing continues.
    """
    path, output = Path(path).resolve(), Path(output).resolve()
    if output.suffix.lower() != ".md" or path == output:
        raise ValueError("辅助输出必须是独立的 .md 文件。")
    pages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "schema_version": 1, "method": "local_vlm_whole_page_text_aid", "source": str(path),
        "notice": NOTICE, "processing_complete": False, "requires_original_review": True,
        "requested_dpi": dpi, "max_render_edge": MAX_EDGE, "max_render_pixels": MAX_PIXELS,
        "max_pages": MAX_PAGES, "page_count": None,
    }
    document = None
    try:
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError("辅助识别输入必须为本地 PDF 文件。")
        if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError("PDF 为空或超过 200 MiB 限制。")
        with path.open("rb") as stream:
            if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("输入不是可识别的 PDF 文件。")
        render_scale(72, 72, dpi)  # Validate configuration before allocating/model loading.
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        metadata["page_count"] = len(document)
        validate_page_count(len(document))
        _write_outputs(output, pages, metadata)
        predictor = _local_predictor()
        total_chars = 0
        for index in range(len(document)):
            record: dict[str, Any] = {"page": index + 1, "status": "error", "warnings": [], "chars": 0}
            page = bitmap = image = None
            try:
                page = document[index]
                scale = render_scale(*page.get_size(), dpi)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil().convert("RGB")
                record["render_size"] = list(image.size)
                record["effective_dpi"] = round(scale * 72, 2)
                if max(image.size) > MAX_EDGE or image.width * image.height > MAX_PIXELS:
                    raise ValueError("页面渲染超过像素限制。")
                types = layout_types(predictor.layout_detect(image))
                record["layout_types"] = types
                if not TEXT_TYPES.intersection(types):
                    record["status"] = "skipped_uncertain"
                    record["warnings"] = ["no_text_candidates" if types else "empty_layout"]
                else:
                    result = predictor.content_extract(image, type="text")
                    text = None if result is None else str(result)
                    warnings = assess_text(text, finish_reason=getattr(result, "finish_reason", None))
                    record["chars"] = len(text or "")
                    if text and total_chars + len(text) > MAX_TOTAL_CHARS:
                        warnings.append("total_output_too_long")
                    record["warnings"] = warnings
                    reject = {"empty_output", "page_output_too_long", "total_output_too_long",
                              "truncated_output", "unexpected_finish"}
                    if not reject.intersection(warnings):
                        record["text"] = text
                        total_chars += len(text or "")
                        record["status"] = "transcribed_with_warnings" if warnings else "transcribed"
            except Exception as exc:
                truncated = "truncat" in str(exc).lower() or "length limit" in str(exc).lower()
                record["warnings"] = ["truncated_output" if truncated else "page_error"]
                record["error_type"] = type(exc).__name__
            finally:
                for resource in (image, bitmap, page):
                    if resource is not None:
                        resource.close()
            pages.append(record)
            _write_outputs(output, pages, metadata)
        metadata["processing_complete"] = True
        metadata["status"] = "completed_with_warnings" if any(p["warnings"] for p in pages) else "completed"
    except Exception as exc:
        metadata["status"] = "failed"
        metadata["document_error"] = {"code": "document_error", "error_type": type(exc).__name__,
                                      "message": str(exc)[:300]}
        raise
    finally:
        if document is not None:
            document.close()
        _write_outputs(output, pages, metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    arguments = parser.parse_args()
    try:
        result = augment_pdf(arguments.input, arguments.output, dpi=arguments.dpi)
    except Exception as exc:
        print(f"辅助识别失败：{type(exc).__name__}: {exc}")
        return 1
    print(json.dumps({key: result[key] for key in ("status", "page_count", "transcribed_pages")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
