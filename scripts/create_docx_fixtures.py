"""Build small OOXML regression fixtures using only synthetic content."""
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


def synthetic_page(index: int) -> bytes:
    """Render a fixture using Pillow's bundled font, without external inputs."""
    marker = f"DOCX-IMAGE-7429-PAGE-{index}"
    image = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    lines = [
        marker,
        "Synthetic OCR fixture. No student records.",
        "",
        f"Exercise {index}",
        "Answer: linear search has worst-case time O(n)."
        if index == 1 else "Answer: binary search requires a sorted array.",
        "Test value: 42.",
    ]
    draw.multiline_text((80, 100), "\n".join(lines), fill="black", font=font, spacing=24)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("fixture_marker", marker)
    with BytesIO() as output:
        image.save(output, format="PNG", pnginfo=metadata)
        return output.getvalue()


def create_fixtures(root: Path) -> None:
    output = root / "artifacts/docx-check"
    output.mkdir(parents=True, exist_ok=True)
    namespaces = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                  'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                  'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
                  'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                  'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                  'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
    text = ('<w:p><w:r><w:t>DOCX 文本和公式识别测试</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>DOCX-TEXT-7429 第一题答案：算法复杂度为 O(n)。</w:t></w:r></w:p>'
            '<w:p><m:oMath><m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e>'
            '<m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup><m:r><m:t>+</m:t></m:r>'
            '<m:f><m:num><m:r><m:t>1</m:t></m:r></m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>'
            '</m:oMath></w:p>'
            '<w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/></w:tblPr>'
            '<w:tblGrid><w:gridCol w:w="4500"/><w:gridCol w:w="4500"/></w:tblGrid>'
            '<w:tr><w:tc><w:p><w:r><w:t>题目</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>答案</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:p><w:r><w:t>第二题</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>DOCX-TABLE-7429 42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    def picture(index):
        return (f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="5000000" cy="6400000"/>'
                f'<wp:docPr id="{index}" name="Synthetic page {index}"/>'
                '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{index}" name="page-{index}.png"/>'
                '<pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="rId{index}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="5000000" cy="6400000"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
                '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    for name, with_text, with_images in [("text-table-math", True, False), ("mixed", True, True), ("images-only", False, True)]:
        content = text if with_text else ""
        if with_images:
            content += picture(1) + '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + picture(2)
        document = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {namespaces}><w:body>{content}'
                    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
                    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
                    '</w:sectPr></w:body></w:document>')
        with ZipFile(output / f"{name}.docx", "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            archive.writestr("word/document.xml", document)
            if with_images:
                rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                for index in (1, 2):
                    rels += f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/page-{index}.png"/>'
                    archive.writestr(f"word/media/page-{index}.png", synthetic_page(index))
                archive.writestr("word/_rels/document.xml.rels", rels + '</Relationships>')
    print(f"Created 3 synthetic DOCX fixtures in {output}")


if __name__ == "__main__":
    create_fixtures(Path(__file__).resolve().parents[1])
