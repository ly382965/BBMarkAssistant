"""Recover OCR image assets without fetching URLs or reading outside a run.

An exported figure/caption is not evidence that the words inside the figure
were recognized. Callers must OCR each unique asset or reject the document.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from PIL import Image


MAX_IMAGES = 64
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_ALT = r"(?:\\.|[^\]\\])*"
_INLINE = re.compile(r"!\[(" + _ALT + r")\]\((<[^>\r\n]*>|[^\r\n)]*)\)")
_REFERENCE = re.compile(r"!\[(" + _ALT + r")\](?:\[([^\]\r\n]*)\])?")
_DEFINITION = re.compile(r"(?m)^[ \t]{0,3}\[([^\]\r\n]+)\]:[ \t]*(.*)$")
_HTML_IMAGE = re.compile(r"<img\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE)
_DATA_IMAGE = re.compile(r"data:image/", re.IGNORECASE)
_DETAILS = re.compile(r"\s*<details>\s*<summary>(text_image|natural_image|flowchart)</summary>\s*(.*?)\s*</details>", re.DOTALL)
_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}


@dataclass(frozen=True)
class ImageOccurrence:
    start: int
    end: int
    source: str


@dataclass(frozen=True)
class ImageDescription:
    """A covered image with a description, but no recognized answer text."""

    text: str


class ImageDescriptionOnlyError(ValueError):
    """A fully checked document contains descriptions and no answer text."""

    def __init__(self, text: str, records: list[dict]):
        super().__init__("OCR 仅得到图像描述，尚无可核验的答案正文；请人工检查，不能据此打零分。")
        self.text = text
        self.records = records


class _ImageTag(HTMLParser):
    def __init__(self, value: str):
        super().__init__(convert_charrefs=True)
        self.source = ""
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            sources = [value for key, value in attrs if key.lower() == "src"]
            if len(sources) != 1 or not sources[0]:
                raise ValueError("OCR 图片缺少唯一的 src，无法确认图片内容已覆盖；请人工检查。")
            self.source = sources[0]


def _destination(value: str) -> str:
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        if end == -1:
            raise ValueError("OCR 图片地址不完整；请人工检查原件。")
        return html.unescape(value[1:end])
    # MinerU escapes spaces/parentheses in file names. An optional Markdown
    # title is metadata, not a path and not a substitute for image OCR.
    match = re.fullmatch(r"(\S+?)(?:\s+[\"'].*[\"'])?", value)
    if not match:
        raise ValueError("OCR 图片地址无法解析；请人工检查原件。")
    return html.unescape(match.group(1))


def _reference_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def image_occurrences(text: str) -> tuple[list[ImageOccurrence], list[tuple[int, int]]]:
    """Parse supported Markdown/HTML image forms; ambiguous forms fail closed."""
    definitions = {}
    for match in _DEFINITION.finditer(text):
        key = _reference_key(match.group(1))
        definitions.setdefault(key, []).append(match)
    occurrences = []
    occupied = []
    for pattern in (_HTML_IMAGE, _INLINE):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            source = _ImageTag(match.group()).source if pattern is _HTML_IMAGE else _destination(match.group(2))
            occurrences.append(ImageOccurrence(match.start(), match.end(), source))
            occupied.append(match.span())
    used_definitions = set()
    for match in _REFERENCE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        key = _reference_key(match.group(2) or match.group(1))
        matches = definitions.get(key, [])
        if not matches:
            raise ValueError("OCR 留有无法取得的图片占位符，答案可能不完整；请人工检查。")
        if len(matches) != 1:
            raise ValueError("OCR 图片引用定义重复，无法确定对应资源；请人工检查。")
        definition = matches[0]
        occurrences.append(ImageOccurrence(match.start(), match.end(), _destination(definition.group(2))))
        used_definitions.add(definition.span())
    return sorted(occurrences, key=lambda item: item.start), sorted(used_definitions)


def _image_bytes(source: str, root: Path, bases: list[Path]) -> bytes:
    if _DATA_IMAGE.match(source):
        match = re.fullmatch(r"data:image/(?:png|jpeg|jpg|webp|gif|bmp|tiff);base64,([A-Za-z0-9+/=\s]+)", source, re.IGNORECASE)
        if not match:
            raise ValueError("OCR 内嵌图片编码无效或格式不受支持；请人工检查。")
        encoded = re.sub(r"\s", "", match.group(1))
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("OCR 内嵌图片超过大小限制；请人工检查。")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("OCR 内嵌图片编码损坏；请人工检查。") from exc
    else:
        decoded = unquote(source)
        parts = urlsplit(decoded)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or decoded.startswith(("/", "\\")):
            raise ValueError("OCR 图片引用不是本轮输出目录内的本地资源；未访问外部地址，请人工检查。")
        relative = Path(decoded.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or ":" in decoded or "\x00" in decoded:
            raise ValueError("OCR 图片路径越出本轮输出目录；请人工检查。")
        candidates = set()
        for base in bases:
            target = base / relative
            if not target.resolve().is_relative_to(root):
                raise ValueError("OCR 图片路径越出本轮输出目录；请人工检查。")
            if any(part.is_symlink() for part in (target, *target.parents) if part.is_relative_to(root)):
                raise ValueError("OCR 图片路径包含链接；请人工检查。")
            if target.is_file():
                candidates.add(target.resolve())
        if len(candidates) != 1:
            raise ValueError("OCR 图片文件缺失或引用不唯一，答案可能不完整；请人工检查。")
        target = candidates.pop()
        if target.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("OCR 图片超过大小限制；请人工检查。")
        payload = target.read_bytes()
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("OCR 图片为空或超过大小限制；请人工检查。")
    return payload


def _image_extension(payload: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as decoded:
                if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
                    raise ValueError("OCR 图片像素过多；请人工检查。")
                if getattr(decoded, "n_frames", 1) != 1:
                    raise ValueError("OCR 图片包含多帧，无法确认所有页面已覆盖；请人工检查。")
                extension = _FORMATS.get(decoded.format)
                if not extension:
                    raise ValueError("OCR 图片格式不受支持；请人工检查。")
                decoded.verify()
                return extension
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("OCR 图片损坏或像素过多；请人工检查。") from exc


def recover_images(
    text: str,
    root: Path,
    bases: list[Path],
    recognize: Callable[[Path, int], str | ImageDescription],
) -> tuple[str, list[dict]]:
    """Replace figures in place, recognizing identical assets only once."""
    occurrences, definitions = image_occurrences(text)
    if len(occurrences) > MAX_IMAGES:
        raise ValueError(f"OCR 输出超过 {MAX_IMAGES} 个图片引用；请人工检查或拆分作业。")
    root = root.resolve()
    bases = [base.resolve() for base in bases]
    if any(not base.is_relative_to(root) for base in bases):
        raise ValueError("OCR 图片资源目录越界；请人工检查。")
    replacements = [(start, end, "") for start, end in definitions]
    validation_replacements = list(replacements)
    records = []
    seen: dict[str, int] = {}
    total = 0
    for index, occurrence in enumerate(occurrences, 1):
        payload = _image_bytes(occurrence.source, root, bases)
        digest = hashlib.sha256(payload).hexdigest()
        previous = seen.get(digest)
        details = _DETAILS.match(text, occurrence.end)
        if details and (not re.search(r"[\w\u4e00-\u9fff]", details.group(2))
                        or re.search(r"<img\b|!\[|data:image/|<details", details.group(2), re.IGNORECASE)):
            details = None
        end = details.end() if details else occurrence.end
        if previous is None:
            total += len(payload)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("OCR 图片总大小超过限制；请人工检查或拆分作业。")
            extension = _image_extension(payload)
            image_path = root / "recovered-images" / f"{index:03d}-{digest[:12]}{extension}"
            if image_path.is_symlink() or image_path.parent.is_symlink() or not image_path.resolve().is_relative_to(root):
                raise ValueError("OCR 图片保存目录包含外部链接；请人工检查。")
            image_path.parent.mkdir(exist_ok=True)
            image_path.write_bytes(payload)
            record = {"index": index, "sha256": digest, "path": str(image_path)}
            if details:
                kind, recognized = details.group(1), details.group(2).strip()
                label = "图像描述，仅供核对，可能存在未识别答案" if kind == "natural_image" else "已有图片识别正文"
                replacement = f"\n[图片 {index} {label}]\n{recognized}\n[图片 {index} 识别结束]\n"
                warning = (
                    f"图片 {index}：图像描述仅供核对，可能存在未识别答案，不能据描述缺失判定学生答错。"
                    if kind == "natural_image" else
                    f"图片 {index}：已保留 MinerU 的图片识别内容；文字、箭头和图形关系仍需对照原图核对。"
                )
                record.update({"method": "provider_image_content", "image_type": kind, "warning": warning})
            else:
                result = recognize(image_path, index)
                description_only = isinstance(result, ImageDescription)
                recognized = (result.text if description_only else result).strip()
                label = "图像描述，仅供核对，可能存在未识别答案" if description_only else "识别正文"
                replacement = f"\n[图片 {index} {label}]\n{recognized}\n[图片 {index} 识别结束]\n"
                record["method"] = "supplemental_ocr"
                if description_only:
                    record.update({
                        "image_type": "natural_image",
                        "warning": f"图片 {index}：补识别仅得到图像描述，可能存在未识别答案，不能据描述缺失判定学生答错。",
                    })
            seen[digest] = index
        else:
            replacement = f"\n[图片 {index} 与图片 {previous} 内容相同，请参照前面的识别正文，勿重复计为另一份答案。]\n"
            record = {"index": index, "sha256": digest, "same_as": previous}
        replacements.append((occurrence.start, end, replacement))
        validation_replacements.append((occurrence.start, end, "" if record.get("image_type") == "natural_image" or previous else replacement))
        records.append(record)
    validation = text
    for start, end, replacement in sorted(validation_replacements, reverse=True):
        validation = validation[:start] + replacement + validation[end:]
    description_only = occurrences and not re.search(r"\w", re.sub(r"<[^>]*>", "", validation))
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    if _DATA_IMAGE.search(text) or re.search(r"<img\b|!\[", text, re.IGNORECASE):
        raise ValueError("OCR 正文仍含未覆盖的图片内容；请人工检查，不能据此判定学生答错。")
    if description_only:
        raise ImageDescriptionOnlyError(text, records)
    return text, records
