"""Small synthetic OpenXML packages; no user documents or external network."""

import struct
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from bb_assistant import docx_package as package
from bb_assistant.docx_package import extract_docx_images


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
V = "urn:schemas-microsoft-com:vml"
PNG = b"\x89PNG\r\n\x1a\nsynthetic raster fixture"
JPEG = b"\xff\xd8\xffsynthetic JPEG fixture"


def document(body="", root="document"):
    return f'<w:{root} xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{R}" xmlns:v="{V}">{body}</w:{root}>'


def relationships(*entries):
    body = ""
    for rid, target, kind, mode in entries:
        body += (
            f"<Relationship Id={quoteattr(rid)} Target={quoteattr(target)} "
            f'Type="{R}/{kind}" TargetMode="{mode}"/>'
        )
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + body
        + "</Relationships>"
    )


def image_ref(rid="rId1"):
    return f'<w:drawing><a:blip r:embed="{rid}"/></w:drawing>'


def write_docx(tmp_path, body="", rels=(), extra=None, content_types_extra="", main_type=None):
    path = tmp_path / "answer.docx"
    main_type = (
        main_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/word/document.xml" ContentType="{main_type}"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="jpg" ContentType="image/jpeg"/>' + content_types_extra + "</Types>"
    )
    parts = {
        "[Content_Types].xml": types,
        "word/document.xml": document(body),
        "word/_rels/document.xml.rels": relationships(*rels),
        **(extra or {}),
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return path


def test_body_table_and_reused_images_deduplicate_bytes_keep_references(tmp_path):
    path = write_docx(
        tmp_path,
        image_ref() + "<w:tbl><w:tr><w:tc>" + image_ref("rId2") + "</w:tc></w:tr></w:tbl>" + image_ref(),
        rels=(("rId1", "media/1.png", "image", "Internal"), ("rId2", "media/copy.png", "image", "Internal")),
        extra={"word/media/1.png": PNG, "word/media/copy.png": PNG, "word/media/unused.png": b"ignored"},
    )
    images = extract_docx_images(path, tmp_path / "images")
    assert len(images) == 1
    assert images[0].path.read_bytes() == PNG
    assert images[0].path.name == "docx-image-0001.png"
    assert images[0].references == (
        ("word/document.xml", "rId1", 1),
        ("word/document.xml", "rId2", 2),
        ("word/document.xml", "rId1", 3),
    )


def test_header_footer_and_notes_use_own_relationships(tmp_path):
    body = (
        '<w:headerReference r:id="head"/><w:footerReference r:id="foot"/>'
        '<w:footnoteReference w:id="1"/><w:endnoteReference w:id="2"/>'
    )
    extra = {}
    rels = []
    for ordinal, (rid, kind, root) in enumerate(
        (
            ("head", "header", "hdr"),
            ("foot", "footer", "ftr"),
            ("notes", "footnotes", "footnotes"),
            ("end", "endnotes", "endnotes"),
        )
    ):
        target = f"{kind}.xml"
        rels.append((rid, target, kind, "Internal"))
        extra[f"word/{target}"] = document(image_ref(), root)
        extra[f"word/_rels/{target}.rels"] = relationships(
            ("rId1", f"media/{ordinal}.png", "image", "Internal")
        )
        extra[f"word/media/{ordinal}.png"] = PNG + bytes([ordinal])
    # An orphaned header is not displayed in the document and must not be OCRed.
    extra["word/header99.xml"] = document(image_ref("missing"), "hdr")
    path = write_docx(tmp_path, body, rels=rels, extra=extra)
    images = extract_docx_images(path, tmp_path / "images")
    assert {image.source_part for image in images} == {
        "word/header.xml",
        "word/footer.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
    }
    assert len(images) == 4


def test_legacy_vml_and_parent_relative_package_path(tmp_path):
    path = write_docx(
        tmp_path,
        '<w:pict><v:imagedata r:id="vml"/></w:pict>',
        rels=(("vml", "../media/answer%20scan.dat", "image", "Internal"),),
        extra={"media/answer scan.dat": JPEG},
    )
    images = extract_docx_images(path, tmp_path / "images")
    assert images[0].path.suffix == ".jpg"
    assert images[0].relationship_id == "vml"


@pytest.mark.parametrize(
    "target",
    [
        "../../escape.png",
        "%2e%2e/%2e%2e/escape.png",
        "C:/a.png",
        "..\\a.png",
        "https://example.invalid/a.png",
        "//example.invalid/a.png",
        "media/a.png?secret=value",
    ],
)
def test_invalid_targets_fail_without_writing_images(tmp_path, target):
    path = write_docx(tmp_path, image_ref(), rels=(("rId1", target, "image", "Internal"),))
    with pytest.raises(ValueError, match="路径"):
        extract_docx_images(path, tmp_path / "images")
    assert not (tmp_path / "images").exists()


def test_opc_rooted_part_uri_resolves_inside_package(tmp_path):
    path = write_docx(
        tmp_path,
        image_ref(),
        rels=(("rId1", "/word/media/answer.png", "image", "Internal"),),
        extra={"word/media/answer.png": PNG},
    )
    assert extract_docx_images(path, tmp_path / "images")[0].path.read_bytes() == PNG


def test_external_relationship_and_linked_image_are_rejected(tmp_path):
    path = write_docx(
        tmp_path, image_ref(), rels=(("rId1", "https://example.invalid/a.png", "image", "External"),)
    )
    with pytest.raises(ValueError, match="外部"):
        extract_docx_images(path, tmp_path / "images")
    path = write_docx(tmp_path, '<a:blip r:link="external"/>')
    with pytest.raises(ValueError, match="外部"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize("rels", [(), (("rId1", "media/absent.png", "image", "Internal"),)])
def test_missing_image_or_relationship_fail(tmp_path, rels):
    path = write_docx(tmp_path, image_ref(), rels=rels)
    with pytest.raises(ValueError, match="缺失"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize(
    "payload", [b"not a zip", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old doc", b"PK\x03\x04broken zip"]
)
def test_renamed_legacy_doc_or_corrupt_zip_is_actionable(tmp_path, payload):
    path = tmp_path / "answer.docx"
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="旧版 .doc"):
        extract_docx_images(path, tmp_path / "images")


def test_arbitrary_zip_is_not_docx(tmp_path):
    path = tmp_path / "answer.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("whatever", "hello")
    with pytest.raises(ValueError, match="有效的 Word"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize("part", ["word/document.xml", "[Content_Types].xml", "word/_rels/document.xml.rels"])
def test_dtd_or_entity_definitions_are_rejected(tmp_path, part):
    path = write_docx(tmp_path, extra={part: '<!DOCTYPE a [<!ENTITY x "expanded">]><a>&x;</a>'})
    with pytest.raises(ValueError, match="实体"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize(
    "limit_name,limit",
    [
        ("MAX_FILE_BYTES", 1),
        ("MAX_ENTRIES", 1),
        ("MAX_EXPANDED_BYTES", 1),
        ("MAX_XML_BYTES", 1),
        ("MAX_IMAGE_BYTES", 1),
        ("MAX_IMAGES", 0),
        ("MAX_TOTAL_IMAGE_BYTES", 1),
        ("MAX_IMAGE_REFERENCES", 0),
    ],
)
def test_processing_limits_are_enforced(tmp_path, monkeypatch, limit_name, limit):
    path = write_docx(
        tmp_path,
        image_ref(),
        rels=(("rId1", "media/1.png", "image", "Internal"),),
        extra={"word/media/1.png": PNG},
    )
    monkeypatch.setattr(package, limit_name, limit)
    with pytest.raises(ValueError, match="上限"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "word/../../escape"])
def test_archive_member_names_cannot_escape(tmp_path, name):
    path = write_docx(tmp_path, extra={name: b"content"})
    with pytest.raises(ValueError, match="路径"):
        extract_docx_images(path, tmp_path / "images")
    assert not (tmp_path / "escape").exists()


def test_archive_backslash_name_is_rejected(tmp_path):
    path = write_docx(tmp_path, extra={"word/invalid": b"content"})
    # ZipFile normalizes backslashes on Windows when writing: mutate raw name bytes.
    path.write_bytes(path.read_bytes().replace(b"word/invalid", b"word\\invalid"))
    with pytest.raises(ValueError, match="路径"):
        extract_docx_images(path, tmp_path / "images")


def test_duplicate_zip_members_rejected(tmp_path):
    path = write_docx(tmp_path)
    with pytest.warns(UserWarning), ZipFile(path, "a") as archive:
        archive.writestr("word/document.xml", document())
    with pytest.raises(ValueError, match="重复"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize(
    "data,extension",
    [
        (PNG, ".png"),
        (JPEG, ".jpg"),
        (b"GIF89a123", ".gif"),
        (b"BM123", ".bmp"),
        (b"II*\x00123", ".tiff"),
        (b"MM\x00*123", ".tiff"),
        (b"RIFF1234WEBP123", ".webp"),
    ],
)
def test_raster_type_detected_from_bytes_not_filename(tmp_path, data, extension):
    path = write_docx(
        tmp_path,
        image_ref(),
        rels=(("rId1", "media/image.unknown", "image", "Internal"),),
        extra={"word/media/image.unknown": data},
    )
    assert extract_docx_images(path, tmp_path / "images")[0].path.suffix == extension


@pytest.mark.parametrize(
    "data,mime",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        (b"\xd7\xcd\xc6\x9aWMF", "image/x-wmf"),
        (b"fake EMF", "image/x-emf"),
        (b"corrupt image", "image/png"),
    ],
)
def test_unsupported_images_require_pdf_and_never_silently_disappear(tmp_path, data, mime):
    path = write_docx(
        tmp_path,
        image_ref(),
        rels=(("rId1", "media/image.bin", "image", "Internal"),),
        extra={"word/media/image.bin": data},
        content_types_extra=f'<Default Extension="bin" ContentType="{mime}"/>',
    )
    with pytest.raises(ValueError, match="PDF"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize(
    "body",
    [
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office"/>',
        '<w:altChunk r:id="chunk"/>',
        '<w:control r:id="control"/>',
    ],
)
def test_embedded_objects_fail_closed(tmp_path, body):
    path = write_docx(tmp_path, body)
    with pytest.raises(ValueError, match="嵌入对象"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize(
    "body",
    [
        '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="chart"/>',
        '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" r:dm="data"/>',
    ],
)
def test_chart_and_smartart_never_silently_disappear(tmp_path, body):
    path = write_docx(tmp_path, body)
    with pytest.raises(ValueError, match="图表或 SmartArt"):
        extract_docx_images(path, tmp_path / "images")


def test_ordinary_omml_and_empty_document_remain_native_parser_responsibility(tmp_path):
    path = write_docx(
        tmp_path,
        '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x=1</m:t></m:r></m:oMath>',
    )
    assert extract_docx_images(path, tmp_path / "images") == []
    path = write_docx(tmp_path)
    assert extract_docx_images(path, tmp_path / "images") == []


def test_preexisting_output_is_never_overwritten(tmp_path):
    path = write_docx(
        tmp_path,
        image_ref(),
        rels=(("rId1", "media/1.png", "image", "Internal"),),
        extra={"word/media/1.png": PNG},
    )
    directory = tmp_path / "images"
    directory.mkdir()
    output = directory / "docx-image-0001.png"
    output.write_bytes(b"preserve existing")
    with pytest.raises(ValueError, match="同名文件"):
        extract_docx_images(path, directory)
    assert output.read_bytes() == b"preserve existing"


def test_macro_document_disguised_as_docx_is_rejected(tmp_path):
    path = write_docx(tmp_path, main_type="application/vnd.ms-word.document.macroEnabled.main+xml")
    with pytest.raises(ValueError, match="宏"):
        extract_docx_images(path, tmp_path / "images")


MATH = '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x=1</m:t></m:r></m:oMath>'


@pytest.mark.parametrize("kind", ["footnotes", "endnotes"])
@pytest.mark.parametrize("content", ["<w:p><w:r><w:t>作业答案</w:t></w:r></w:p>", MATH])
def test_referenced_note_text_or_math_cannot_silently_drop(tmp_path, kind, content):
    path = write_docx(
        tmp_path,
        f'<w:{kind[:-1]}Reference w:id="1"/>',
        rels=(("notes", kind + ".xml", kind, "Internal"),),
        extra={f"word/{kind}.xml": document(content, kind)},
    )
    with pytest.raises(ValueError, match="脚注或尾注.*PDF"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize("kind,root", [("header", "hdr"), ("footer", "ftr")])
@pytest.mark.parametrize(
    "content", ["<w:tbl><w:tr><w:tc><w:p><w:r><w:t>作业答案</w:t></w:r></w:p></w:tc></w:tr></w:tbl>", MATH]
)
def test_referenced_header_footer_table_or_math_cannot_silently_drop(tmp_path, kind, root, content):
    path = write_docx(
        tmp_path,
        f'<w:{kind}Reference r:id="part"/>',
        rels=(("part", kind + ".xml", kind, "Internal"),),
        extra={f"word/{kind}.xml": document(content, root)},
    )
    with pytest.raises(ValueError, match="页眉或页脚.*PDF"):
        extract_docx_images(path, tmp_path / "images")


@pytest.mark.parametrize("kind,root", [("header", "hdr"), ("footer", "ftr")])
def test_plain_header_footer_paragraphs_remain_supported(tmp_path, kind, root):
    path = write_docx(
        tmp_path,
        f'<w:{kind}Reference r:id="part"/>',
        rels=(("part", kind + ".xml", kind, "Internal"),),
        extra={f"word/{kind}.xml": document("<w:p><w:r><w:t>作业名称</w:t></w:r></w:p>", root)},
    )
    assert extract_docx_images(path, tmp_path / "images") == []


def test_unreferenced_note_content_does_not_block_document(tmp_path):
    path = write_docx(
        tmp_path,
        "<w:p><w:r><w:t>有效正文</w:t></w:r></w:p>",
        extra={"word/footnotes.xml": document(MATH, "footnotes")},
    )
    assert extract_docx_images(path, tmp_path / "images") == []


def synthetic_equation_ole(*, class_name=b"Equation.3", version=0x00020000, mtef_version=3):
    """Build a minimal CFB with two ordinary streams, not student data."""
    free, end, fat = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 0, *([free] * 108))
    chain = [fat, end, *range(3, 10), end, *range(11, 18), end]
    fat_sector = struct.pack("<128I", *chain, *([free] * (128 - len(chain))))

    def entry(name, kind, start, size, *, child=free, right=free):
        data = bytearray(128)
        encoded = (name + "\0").encode("utf-16-le")
        data[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", data, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", data, 116, start, size)
        return data

    directory = (
        entry("Root Entry", 5, end, 0, child=1)
        + entry("Equation Native", 2, 2, 4096, right=2)
        + entry("\x01CompObj", 2, 10, 4096)
        + bytearray(128)
    )
    native = bytearray(4096)
    struct.pack_into("<HIHI", native, 0, 28, version, 0, 5)
    native[28:33] = bytes([mtef_version, 0, 0, 0, 0])
    compobj = class_name.ljust(4096, b"\0")
    return bytes(header) + fat_sector + directory + native + compobj


def synthetic_wmf():
    header = bytearray(44)
    header[:6] = b"\xd7\xcd\xc6\x9a\x00\x00"
    struct.pack_into("<hhhhH", header, 6, 0, 0, 1440, 720, 1440)
    header[22:26] = b"\x01\x00\x09\x00"
    return bytes(header)


def legacy_equation_docx(tmp_path, *, body=None, payload=None, preview=None, rels=None):
    if body is None:
        body = (
            '<w:object><v:shape id="shape1"><v:imagedata r:id="preview"/></v:shape>'
            '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
            'Type="Embed" DrawAspect="Content" ShapeID="shape1" ProgID="Unknown" '
            'r:id="equation"/></w:object>'
        )
    return write_docx(
        tmp_path,
        body,
        rels=rels if rels is not None else (
            ("equation", "embeddings/equation.bin", "oleObject", "Internal"),
            ("preview", "media/equation.emf", "image", "Internal"),
        ),
        extra={
            "word/embeddings/equation.bin": payload if payload is not None else synthetic_equation_ole(),
            "word/media/equation.emf": preview if preview is not None else synthetic_wmf(),
        },
        content_types_extra='<Default Extension="emf" ContentType="image/x-emf"/>',
    )


def test_legacy_equation_requires_opt_in_decoder_and_collector(tmp_path):
    path = legacy_equation_docx(tmp_path)
    for kwargs in ({}, {"equation_decoder": lambda _: "x=1"}, {"equations": []}):
        with pytest.raises(ValueError, match="嵌入对象"):
            extract_docx_images(path, tmp_path / "images", **kwargs)


def test_legacy_equation_decodes_bounded_cfb_and_skips_only_its_preview(tmp_path):
    body = (
        '<w:object><v:shape id="shape1"><v:imagedata r:id="preview"/></v:shape>'
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'Type="Embed" DrawAspect="Content" ShapeID="shape1" ProgID="Unknown" '
        'r:id="equation"/></w:object>' + image_ref("other")
    )
    path = legacy_equation_docx(tmp_path, body=body, rels=(
        ("equation", "embeddings/equation.bin", "oleObject", "Internal"),
        ("preview", "media/equation.emf", "image", "Internal"),
        ("other", "media/answer.png", "image", "Internal"),
    ))
    with ZipFile(path, "a") as archive:
        archive.writestr("word/media/answer.png", PNG)
    equations, called = [], []

    def decode(payload):
        called.append(payload)
        return " x=1 "

    images = extract_docx_images(path, tmp_path / "images", equation_decoder=decode, equations=equations)
    assert called == [synthetic_equation_ole()]
    assert equations == [package.DocxEquation("x=1", "word/document.xml", "equation")]
    assert len(images) == 1 and images[0].relationship_id == "other"


def test_legacy_equation_preview_fallback_uses_magic_despite_wrong_extension(tmp_path, monkeypatch):
    path = legacy_equation_docx(tmp_path)
    rendered = []

    def render(payload):
        rendered.append(payload)
        return PNG

    monkeypatch.setattr(package, "_render_equation_preview", render)
    equations = []
    images = extract_docx_images(
        path, tmp_path / "images", equation_decoder=lambda _: None, equations=equations
    )
    assert not equations
    assert rendered == [synthetic_wmf()]
    assert images[0].path.suffix == ".png" and images[0].path.read_bytes() == PNG


@pytest.mark.parametrize(
    "latex", ["", "  ", "x\0y", object(), "x" * 100_001],
    ids=["empty", "whitespace", "control", "wrong_type", "oversized"],
)
def test_invalid_legacy_equation_decoder_result_uses_preview(tmp_path, monkeypatch, latex):
    path = legacy_equation_docx(tmp_path)
    monkeypatch.setattr(package, "_render_equation_preview", lambda _: PNG)
    equations = []
    assert len(extract_docx_images(
        path, tmp_path / "images", equation_decoder=lambda _: latex, equations=equations
    )) == 1
    assert equations == []


@pytest.mark.parametrize("mutation", ["wrong_shape", "linked", "icon", "missing_shape", "second_ole"])
def test_legacy_equation_requires_exact_local_content_preview(tmp_path, mutation):
    body = (
        '<w:object><v:shape id="shape1"><v:imagedata r:id="preview"/></v:shape>'
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'Type="Embed" DrawAspect="Content" ShapeID="shape1" ProgID="Equation.3" '
        'r:id="equation"/></w:object>'
    )
    if mutation == "wrong_shape":
        body = body.replace('ShapeID="shape1"', 'ShapeID="another"')
    elif mutation == "linked":
        body = body.replace('Type="Embed"', 'Type="Link"')
    elif mutation == "icon":
        body = body.replace('DrawAspect="Content"', 'DrawAspect="Icon"')
    elif mutation == "missing_shape":
        body = body.replace('<v:shape id="shape1"><v:imagedata r:id="preview"/></v:shape>', "")
    else:
        body = body.replace('</w:object>', '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office"/></w:object>')
    path = legacy_equation_docx(tmp_path, body=body)
    with pytest.raises(ValueError, match="嵌入对象"):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=[])


@pytest.mark.parametrize("rid", ["equation", "preview"])
def test_external_or_missing_equation_parts_remain_blocked(tmp_path, rid):
    rels = [
        ("equation", "embeddings/equation.bin", "oleObject", "Internal"),
        ("preview", "media/equation.emf", "image", "Internal"),
    ]
    rels = [(name, target, kind, "External" if name == rid else mode) for name, target, kind, mode in rels]
    path = legacy_equation_docx(tmp_path, rels=rels)
    with pytest.raises(ValueError, match="外部"):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=[])
    path = legacy_equation_docx(tmp_path, rels=[entry for entry in rels if entry[0] != rid])
    with pytest.raises(ValueError, match="缺失"):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=[])


@pytest.mark.parametrize("payload", [
    b"not CFB",
    synthetic_equation_ole(class_name=b"Excel.Sheet", version=0),
    synthetic_equation_ole(mtef_version=99),
], ids=["not_cfb", "wrong_class_and_header", "wrong_mtef_version"])
def test_prog_id_does_not_authorize_unknown_or_invalid_ole(tmp_path, payload):
    path = legacy_equation_docx(tmp_path, payload=payload)
    with pytest.raises(ValueError, match="嵌入对象"):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=[])


@pytest.mark.parametrize("limit_name,limit", [
    ("MAX_OLE_BYTES", 100), ("MAX_EQUATION_BYTES", 100), ("MAX_PREVIEW_PIXELS", 10),
    ("MAX_PREVIEW_DIMENSION", 10), ("MAX_EQUATIONS", 0),
])
def test_equation_limits_are_enforced(tmp_path, monkeypatch, limit_name, limit):
    path = legacy_equation_docx(tmp_path)
    monkeypatch.setattr(package, limit_name, limit)
    with pytest.raises(ValueError):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=[])


def test_decoded_formula_does_not_whitelist_other_vector_reference(tmp_path):
    body = (
        '<w:object><v:shape id="shape1"><v:imagedata r:id="preview"/></v:shape>'
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
        'Type="Embed" DrawAspect="Content" ShapeID="shape1" r:id="equation"/></w:object>'
        + image_ref("preview")
    )
    path = legacy_equation_docx(tmp_path, body=body)
    equations = []
    with pytest.raises(ValueError, match="矢量"):
        extract_docx_images(path, tmp_path / "images", equation_decoder=lambda _: "x=1", equations=equations)
    assert equations == []
