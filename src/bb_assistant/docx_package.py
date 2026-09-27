"""Inspect DOCX packages and extract only document-referenced raster images.

Package contents are data: no relationships are fetched, no objects are executed,
and original archive paths are never used as output filenames.
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException


MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGES = 100
MAX_IMAGE_REFERENCES = 1000
MAX_OLE_BYTES = 8 * 1024 * 1024
MAX_EQUATION_BYTES = 4 * 1024 * 1024
MAX_EQUATIONS = 1000
MAX_EQUATION_LATEX = 100_000
MAX_PREVIEW_PIXELS = 16_000_000
MAX_PREVIEW_DIMENSION = 20_000

_CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORD_NS = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_DRAW_NS = {
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
}
_MATH_NS = {
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
}
_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_VML_NS = "urn:schemas-microsoft-com:vml"
_OFFICE_NS = "urn:schemas-microsoft-com:office:office"
_SVG_NS = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_INVALID = "DOCX 文件损坏或不是有效的 Word 文档；旧版 .doc 不能直接改名，请用 Word 另存为 DOCX 或 PDF。"
_EXPORT_PDF = "请用 Word 将作业导出为 PDF 后重试。"


@dataclass(frozen=True)
class DocxImage:
    path: Path
    source_part: str
    relationship_id: str
    ordinal: int
    # Includes the first occurrence and duplicate references in document order.
    references: tuple[tuple[str, str, int], ...] = ()


@dataclass(frozen=True)
class DocxEquation:
    latex: str
    source_part: str
    relationship_id: str


@dataclass(frozen=True)
class _Relationship:
    target: str
    kind: str
    external: bool


def _tag_parts(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        return tuple(tag[1:].split("}", 1))
    return "", tag


def _relationship_attribute(element, name: str) -> str:
    values = [element.get(f"{{{namespace}}}{name}") for namespace in _REL_NS]
    values = [value for value in values if value is not None]
    if len(values) > 1 and len(set(values)) > 1:
        raise ValueError(_INVALID)
    return values[0] if values else ""


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
        return False
    return all(segment not in {"", ".", ".."} for segment in name.rstrip("/").split("/"))


def _resolve_target(source_part: str, target: str) -> str:
    """Resolve package-relative URI paths without permitting package escapes."""
    try:
        uri = urlsplit(target)
    except ValueError as exc:
        raise ValueError("DOCX 中包含无效的附件路径；" + _EXPORT_PDF) from exc
    if uri.scheme or uri.netloc or uri.query or uri.fragment:
        raise ValueError("DOCX 中包含外部或无效的附件路径；" + _EXPORT_PDF)
    decoded = unquote(uri.path)
    if not decoded or "\\" in decoded or "\x00" in decoded or ":" in decoded or decoded.startswith("//"):
        raise ValueError("DOCX 附件路径超出文档包范围；" + _EXPORT_PDF)
    # OPC absolute part URIs are rooted inside the ZIP package, not the host disk.
    segments = [] if decoded.startswith("/") else posixpath.dirname(source_part).split("/")
    for segment in decoded.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if not segments:
                raise ValueError("DOCX 附件路径超出文档包范围；" + _EXPORT_PDF)
            segments.pop()
        else:
            segments.append(segment)
    resolved = "/".join(segments)
    if not _safe_archive_name(resolved):
        raise ValueError("DOCX 中包含无效的附件路径；" + _EXPORT_PDF)
    return resolved


def _read_bounded(archive: ZipFile, name: str, limit: int) -> bytes:
    try:
        info = archive.getinfo(name)
        if info.file_size > limit:
            raise ValueError("DOCX 中的 XML 或图片大小超出处理上限；" + _EXPORT_PDF)
        with archive.open(info) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("DOCX 中的 XML 或图片大小超出处理上限；" + _EXPORT_PDF)
        return content
    except KeyError as exc:
        raise ValueError("DOCX 引用的图片或文档部件缺失；" + _EXPORT_PDF) from exc


def _xml(archive: ZipFile, name: str):
    try:
        return ElementTree.fromstring(
            _read_bounded(archive, name, MAX_XML_BYTES),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ElementTree.ParseError) as exc:
        raise ValueError("DOCX XML 无效或包含不支持的实体定义；" + _EXPORT_PDF) from exc


def _relationships(archive: ZipFile, source_part: str) -> dict[str, _Relationship]:
    directory, filename = posixpath.split(source_part)
    rels_name = posixpath.join(directory, "_rels", filename + ".rels")
    if rels_name not in archive.namelist():
        return {}
    root = _xml(archive, rels_name)
    if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
        raise ValueError(_INVALID)
    result = {}
    for element in root:
        if element.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            raise ValueError(_INVALID)
        rid = element.get("Id", "")
        if not rid or rid in result or not element.get("Target") or not element.get("Type"):
            raise ValueError(_INVALID)
        mode = element.get("TargetMode", "Internal")
        if mode not in {"Internal", "External"}:
            raise ValueError(_INVALID)
        result[rid] = _Relationship(element.attrib["Target"], element.attrib["Type"], mode == "External")
    return result


def _linked_part(source_part: str, rid: str, relationships: dict, expected_kind: str) -> str:
    relationship = relationships.get(rid)
    if relationship is None:
        raise ValueError("DOCX 引用的图片或文档部件关系缺失；" + _EXPORT_PDF)
    if relationship.external:
        raise ValueError("DOCX 包含外部链接图片或文档部件，无法保证作业完整；" + _EXPORT_PDF)
    if not any(relationship.kind == f"{namespace}/{expected_kind}" for namespace in _REL_NS):
        raise ValueError("DOCX 图片或文档部件关系类型无效；" + _EXPORT_PDF)
    return _resolve_target(source_part, relationship.target)


def _raster_extension(content: bytes, declared_type: str) -> str:
    # Reject vector formats even if their extension or MIME type was disguised.
    if declared_type.lower() in {"image/svg+xml", "image/x-wmf", "image/wmf", "image/x-emf", "image/emf"}:
        raise ValueError("DOCX 包含暂不支持的 SVG、WMF 或 EMF 矢量图片；" + _EXPORT_PDF)
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"BM"):
        return ".bmp"
    if content.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return ".tiff"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("DOCX 中的图片格式无法识别或暂不支持（如 SVG、WMF、EMF）；" + _EXPORT_PDF)


def _check_native_part_support(root) -> None:
    """Do not send incomplete native-parser output to the grader.

    MinerU handles main-document text/tables/OMML and plain header/footer
    paragraphs, but drops note text and header/footer tables or math. Images
    in these parts are handled separately by this module and can pass through.
    """
    _, part_kind = _tag_parts(root.tag)
    if part_kind not in {"hdr", "ftr", "footnotes", "endnotes"}:
        return
    is_note = part_kind in {"footnotes", "endnotes"}
    for element in root.iter():
        namespace, local_name = _tag_parts(element.tag)
        math = namespace in _MATH_NS and local_name in {"oMath", "oMathPara"}
        if is_note:
            text = namespace in _WORD_NS and (
                (local_name in {"t", "delText", "instrText"} and (element.text or "").strip())
                or local_name == "sym"
            )
            if math or text:
                raise ValueError("DOCX 的脚注或尾注包含文字或公式，当前无法完整识别；" + _EXPORT_PDF)
        elif math or (namespace in _WORD_NS and local_name == "tbl"):
            raise ValueError("DOCX 的页眉或页脚包含表格或公式，当前无法完整识别；" + _EXPORT_PDF)


def _is_legacy_equation(payload: bytes) -> bool:
    """Identify an Equation Native stream without loading an OLE application."""
    if len(payload) > MAX_OLE_BYTES or not payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return False
    try:
        import olefile

        with olefile.OleFileIO(io.BytesIO(payload), raise_defects=olefile.DEFECT_INCORRECT) as archive:
            streams = archive.listdir(streams=True, storages=False)
            if len(streams) > 128 or ["Equation Native"] not in streams:
                return False
            sizes = [archive.get_size(part) for part in streams]
            if any(size > MAX_OLE_BYTES for size in sizes) or sum(sizes) > MAX_OLE_BYTES:
                return False
            size = archive.get_size("Equation Native")
            if not 33 <= size <= MAX_EQUATION_BYTES:
                return False
            native = archive.openstream("Equation Native").read(MAX_EQUATION_BYTES + 1)
            header_size, version, _clipboard, object_size = struct.unpack_from("<HIHI", native)
            if not (28 <= header_size <= len(native) and 5 <= object_size <= MAX_EQUATION_BYTES):
                return False
            if header_size + object_size > len(native):
                return False
            # MTEF versions produced by Equation Editor 3 and MathType. The
            # decoder separately checks every record and full consumption.
            if native[header_size] not in {3, 5}:
                return False
            class_info = b""
            if ["\x01CompObj"] in streams:
                if archive.get_size("\x01CompObj") > 64 * 1024:
                    return False
                class_info = archive.openstream("\x01CompObj").read(64 * 1024 + 1)
            known_class = any(
                marker in class_info
                for marker in (b"Equation.3", b"Equation.DSMT4", b"MathType")
            )
            return known_class or version == 0x00020000
    except (ImportError, OSError, ValueError, EOFError, IndexError, struct.error):
        return False


def _metafile_size(content: bytes, dpi: int = 300) -> tuple[int, int] | None:
    """Read physical dimensions before asking the Windows renderer to allocate."""
    if content.startswith(b"\xd7\xcd\xc6\x9a\x00\x00") and len(content) >= 44:
        x0, y0, x1, y1, inch = struct.unpack_from("<hhhhH", content, 6)
        if inch <= 0:
            raise ValueError("DOCX 公式预览图片尺寸无效；" + _EXPORT_PDF)
        size = (int((x1 - x0) * dpi / inch), int((y1 - y0) * dpi / inch))
    elif len(content) >= 88 and content[:4] == b"\x01\x00\x00\x00" and content[40:44] == b" EMF":
        x0, y0, x1, y1 = struct.unpack_from("<iiii", content, 24)
        size = (int((x1 - x0) * dpi / 2540), int((y1 - y0) * dpi / 2540))
    else:
        return None
    if min(size) <= 0 or max(size) > MAX_PREVIEW_DIMENSION or size[0] * size[1] > MAX_PREVIEW_PIXELS:
        raise ValueError("DOCX 公式预览图片尺寸超出处理上限；" + _EXPORT_PDF)
    return size


def _render_equation_preview(content: bytes) -> bytes:
    """Rasterize only a validated equation's cached local WMF/EMF preview."""
    if _metafile_size(content) is None:
        raise ValueError("DOCX 公式预览图片格式无法识别；" + _EXPORT_PDF)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            if image.format != "WMF":
                raise ValueError("预览不是 Windows 图元文件")
            image.load(dpi=300)
            if (
                min(image.size) <= 0
                or max(image.size) > MAX_PREVIEW_DIMENSION
                or image.width * image.height > MAX_PREVIEW_PIXELS
            ):
                raise ValueError("公式预览图片尺寸超出处理上限")
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG", dpi=(300, 300))
            if output.tell() > MAX_IMAGE_BYTES:
                raise ValueError("公式预览图片大小超出处理上限")
            return output.getvalue()
    except (OSError, ValueError, SyntaxError, ZeroDivisionError, Image.DecompressionBombError) as exc:
        raise ValueError("DOCX 旧版公式的预览图片无法转换；" + _EXPORT_PDF) from exc


def _legacy_equations(archive, root, source_part, relationships, decoder):
    supported, skipped, previews, equations = set(), set(), set(), []
    parents = {child: element for element in root.iter() for child in element}
    for element in root.iter():
        if element.tag != f"{{{_OFFICE_NS}}}OLEObject":
            continue
        if len(supported) >= MAX_EQUATIONS:
            raise ValueError("DOCX 旧版公式数量超出处理上限；" + _EXPORT_PDF)
        parent = parents.get(element)
        if parent is None or _tag_parts(parent.tag)[0] not in _WORD_NS or _tag_parts(parent.tag)[1] != "object":
            continue
        if element.get("Type") != "Embed" or element.get("DrawAspect") != "Content":
            continue
        if sum(child.tag == f"{{{_OFFICE_NS}}}OLEObject" for child in parent) != 1:
            continue
        shape_id = element.get("ShapeID")
        shapes = [
            child for child in parent
            if child.tag == f"{{{_VML_NS}}}shape" and child.get("id") == shape_id
        ]
        if not shape_id or len(shapes) != 1:
            continue
        images = list(shapes[0].iter(f"{{{_VML_NS}}}imagedata"))
        if len(images) != 1 or images[0].get("src") or _relationship_attribute(images[0], "link"):
            continue
        rid = _relationship_attribute(element, "id")
        target = _linked_part(source_part, rid, relationships, "oleObject")
        payload = _read_bounded(archive, target, MAX_OLE_BYTES)
        if not _is_legacy_equation(payload):
            continue
        image_rid = _relationship_attribute(images[0], "id")
        preview = _linked_part(source_part, image_rid, relationships, "image")
        preview_bytes = _read_bounded(archive, preview, MAX_IMAGE_BYTES)
        # Check the preview even when native decoding succeeds. A missing,
        # external, or disguised preview must never justify bypassing the gate.
        if _metafile_size(preview_bytes) is None:
            _raster_extension(preview_bytes, "")
        try:
            latex = decoder(payload)
        except (OSError, ValueError, RuntimeError, UnicodeError):
            latex = None
        if (
            isinstance(latex, str)
            and latex.strip()
            and len(latex) <= MAX_EQUATION_LATEX
            and not any(ord(char) < 32 and char not in "\t\r\n" for char in latex)
        ):
            equations.append(DocxEquation(latex.strip(), source_part, rid))
            skipped.add(images[0])
        else:
            previews.add(images[0])
        supported.add(element)
    return supported, skipped, previews, equations


def _extract(
    archive: ZipFile,
    output_dir: Path,
    equation_decoder: Callable[[bytes], str | None] | None = None,
    equations: list[DocxEquation] | None = None,
) -> list[DocxImage]:
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
        raise ValueError("DOCX 压缩包内容数量或解压后大小超出处理上限；" + _EXPORT_PDF)
    names = [entry.filename for entry in entries]
    if len(set(names)) != len(names) or any(
        not _safe_archive_name(entry.filename) or not _safe_archive_name(entry.orig_filename)
        for entry in entries
    ):
        raise ValueError("DOCX 包含重复或无效的文档路径；" + _EXPORT_PDF)
    if any(entry.flag_bits & 1 for entry in entries):
        raise ValueError("暂不支持加密的 DOCX 文件；请解除文档密码后重试。")
    if any(
        entry.file_size > MAX_XML_BYTES for entry in entries if entry.filename.endswith((".xml", ".rels"))
    ):
        raise ValueError("DOCX 中的 XML 大小超出处理上限；" + _EXPORT_PDF)
    if "word/document.xml" not in names or "[Content_Types].xml" not in names:
        raise ValueError(_INVALID)
    types = _xml(archive, "[Content_Types].xml")
    if types.tag != f"{{{_CONTENT_NS}}}Types":
        raise ValueError(_INVALID)
    overrides, defaults = {}, {}
    for element in types:
        if element.tag == f"{{{_CONTENT_NS}}}Override":
            name = element.get("PartName", "").lstrip("/")
            if not _safe_archive_name(name) or name in overrides:
                raise ValueError(_INVALID)
            overrides[name] = element.get("ContentType", "")
        elif element.tag == f"{{{_CONTENT_NS}}}Default":
            extension = element.get("Extension", "").lower()
            if not extension or extension in defaults:
                raise ValueError(_INVALID)
            defaults[extension] = element.get("ContentType", "")
        else:
            raise ValueError(_INVALID)
    if overrides.get("word/document.xml") != _MAIN_TYPE:
        raise ValueError("DOCX 主文档类型无效或包含不支持的宏；" + _EXPORT_PDF)

    # Traverse only document parts actually referenced by the main document.
    pending = ["word/document.xml"]
    visited = set()
    references: list[tuple[str, str, int, str, bool]] = []
    decoded_equations = []
    while pending:
        source_part = pending.pop(0)
        if source_part in visited:
            continue
        visited.add(source_part)
        root = _xml(archive, source_part)
        namespace, local_name = _tag_parts(root.tag)
        expected_name = "document" if source_part == "word/document.xml" else None
        if namespace not in _WORD_NS or (expected_name and local_name != expected_name):
            raise ValueError(_INVALID)
        _check_native_part_support(root)
        relationships = _relationships(archive, source_part)
        supported, skipped, previews = set(), set(), set()
        if equation_decoder is not None and equations is not None:
            supported, skipped, previews, part_equations = _legacy_equations(
                archive, root, source_part, relationships, equation_decoder
            )
            decoded_equations.extend(part_equations)
            if len(decoded_equations) > MAX_EQUATIONS:
                raise ValueError("DOCX 旧版公式数量超出处理上限；" + _EXPORT_PDF)
        note_kinds = set()
        for element in root.iter():
            if element in supported or element in skipped:
                continue
            namespace, local_name = _tag_parts(element.tag)
            rid = _relationship_attribute(element, "id")
            if local_name == "OLEObject" or (namespace in _WORD_NS and local_name in {"altChunk", "control"}):
                raise ValueError("DOCX 包含无法完整识别的嵌入对象或外部内容；" + _EXPORT_PDF)
            if (local_name == "chart" and namespace.endswith("/chart")) or (
                local_name == "relIds" and namespace.endswith("/diagram")
            ):
                raise ValueError("DOCX 包含无法完整识别的图表或 SmartArt；" + _EXPORT_PDF)
            if rid and rid in relationships:
                kind = relationships[rid].kind.rsplit("/", 1)[-1]
                if kind in {"oleObject", "package", "control", "aFChunk", "chart", "diagramData"}:
                    raise ValueError("DOCX 包含无法完整识别的嵌入对象；" + _EXPORT_PDF)
            image_rid = ""
            if namespace in _DRAW_NS and local_name == "blip":
                if _relationship_attribute(element, "link"):
                    raise ValueError("DOCX 包含外部链接图片，无法保证作业完整；" + _EXPORT_PDF)
                image_rid = _relationship_attribute(element, "embed")
                if not image_rid:
                    raise ValueError("DOCX 图片缺少内嵌文件关系；" + _EXPORT_PDF)
            elif (namespace == _VML_NS and local_name == "imagedata") or (
                namespace == _SVG_NS and local_name == "svgBlip"
            ):
                image_rid = rid or _relationship_attribute(element, "embed")
                if not image_rid:
                    raise ValueError("DOCX 图片缺少内嵌文件关系；" + _EXPORT_PDF)
            if image_rid:
                target = _linked_part(source_part, image_rid, relationships, "image")
                references.append((source_part, image_rid, len(references) + 1, target, element in previews))
                if len(references) > MAX_IMAGE_REFERENCES:
                    raise ValueError("DOCX 图片引用数量超出处理上限；" + _EXPORT_PDF)
            if namespace in _WORD_NS:
                if local_name in {"headerReference", "footerReference"}:
                    kind = local_name.removesuffix("Reference")
                    pending.append(_linked_part(source_part, rid, relationships, kind))
                elif local_name in {"footnoteReference", "endnoteReference"}:
                    note_kinds.add(local_name.removesuffix("Reference") + "s")
        for kind in sorted(note_kinds):
            found = [
                (rid, relation)
                for rid, relation in relationships.items()
                if relation.kind.endswith("/" + kind)
            ]
            if len(found) != 1:
                raise ValueError("DOCX 的脚注或尾注关系缺失或无效；" + _EXPORT_PDF)
            pending.append(_linked_part(source_part, found[0][0], relationships, kind))

    unique: dict[str, tuple[bytes, str, list[tuple[str, str, int]]]] = {}
    part_hashes: dict[tuple[str, bool], str] = {}
    total_image_bytes = 0
    for source_part, rid, ordinal, target, equation_preview in references:
        key = (target, equation_preview)
        digest = part_hashes.get(key)
        if digest is None:
            content = _read_bounded(archive, target, MAX_IMAGE_BYTES)
            declared = overrides.get(target, defaults.get(posixpath.splitext(target)[1][1:].lower(), ""))
            if equation_preview and _metafile_size(content) is not None:
                content = _render_equation_preview(content)
                extension = ".png"
            else:
                extension = _raster_extension(content, declared)
            digest = hashlib.sha256(content).hexdigest()
            part_hashes[key] = digest
            if digest not in unique:
                total_image_bytes += len(content)
                if len(unique) >= MAX_IMAGES or total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
                    raise ValueError("DOCX 图片数量或总大小超出处理上限；" + _EXPORT_PDF)
                unique[digest] = (content, extension, [])
        unique[digest][2].append((source_part, rid, ordinal))

    result = []
    if unique:
        output_dir.mkdir(parents=True, exist_ok=True)
    for content, extension, occurrences in unique.values():
        source_part, rid, ordinal = occurrences[0]
        output_path = output_dir / f"docx-image-{ordinal:04d}{extension}"
        # Exclusive creation also refuses pre-existing symlinks or accidental reuse.
        try:
            with output_path.open("xb") as stream:
                stream.write(content)
        except FileExistsError as exc:
            raise ValueError("DOCX 图片输出目录包含同名文件；请换用新的处理目录后重试。") from exc
        result.append(DocxImage(output_path, source_part, rid, ordinal, tuple(occurrences)))
    if equations is not None:
        equations.extend(decoded_equations)
    return result


def extract_docx_images(
    path: Path,
    output_dir: Path,
    *,
    equation_decoder: Callable[[bytes], str | None] | None = None,
    equations: list[DocxEquation] | None = None,
) -> list[DocxImage]:
    """Validate a DOCX and return unique referenced images with occurrence metadata.

    Raises ``ValueError`` with an actionable Chinese message when completeness
    cannot be guaranteed. Ordinary Word math (OMML) is left to the text parser.
    Supplying both a decoder and collector enables read-only Equation Editor /
    MathType extraction, with cached local preview images as the OCR fallback.
    """
    path, output_dir = Path(path), Path(output_dir)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("DOCX 文件大小超出处理上限；请拆分文件或导出为 PDF。")
        with ZipFile(path) as archive:
            return _extract(archive, output_dir, equation_decoder, equations)
    except (BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
        raise ValueError(_INVALID) from exc
    except OSError as exc:
        raise ValueError("无法读取 DOCX 或保存内嵌图片；请检查文件是否存在以及目录访问权限。") from exc
