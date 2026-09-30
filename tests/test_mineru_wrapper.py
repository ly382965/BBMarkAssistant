import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "mineru_local", Path(__file__).resolve().parents[1] / "scripts" / "mineru_local.py"
)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


@pytest.mark.parametrize("tier", ["standard", "basic", "advanced", "flash"])
@pytest.mark.parametrize("attached", [False, True])
def test_docx_uses_native_flash_without_mutating_caller(tier, attached):
    option = [f"--tier={tier}"] if attached else ["--tier", tier]
    original = ["parse", "D:/课 程/答案.DOCX", "-o", "result/document.md", *option, "--pages", "all"]
    expected = ["parse", "D:/课 程/答案.DOCX", "-o", "result/document.md"]
    expected += ["--tier=flash"] if attached else ["--tier", "flash"]
    before = list(original)
    assert wrapper.normalize_parse_args(original) == expected
    assert original == before


@pytest.mark.parametrize("suffix", ["pdf", "png", "jpg", "txt", "doc"])
@pytest.mark.parametrize("tier", ["standard", "advanced"])
def test_other_formats_keep_configured_tier(suffix, tier):
    args = ["parse", f"answer.{suffix}", "--tier", tier, "-o", "document.md"]
    assert wrapper.normalize_parse_args(args) == args


@pytest.mark.parametrize("args", [
    [],
    ["version"],
    ["models", "download", "answer.docx"],
    ["api-server", "--tier", "standard"],
    ["parse", "answer.pdf", "--output", "output.docx", "--tier", "standard"],
    ["parse", "answer.pdf", "--output=output.docx", "--tier=standard"],
    ["parse", "answer.pdf", "-ooutput.docx", "--tier", "standard"],
    ["parse", "answer.pdf", "--api-key", "key.docx", "--tier", "standard"],
    ["parse", "answer.pdf", "--remote-url=http://localhost/example.docx", "--tier", "standard"],
    ["parse", "one.docx", "two.docx", "-o", "output", "--tier", "standard"],
    ["parse", "one.docx", "two.pdf", "-o", "output", "--tier", "standard"],
    ["parse", "-o", "answer.docx"],
    ["parse", "answer.docx", "--unknown-option", "value"],
    ["parse", "answer.docx", "--tier"],
    ["parse", "answer.docx", "--tier", "--output", "out.md"],
    ["parse", "answer.docx", "-o"],
])
def test_options_nonparse_and_incomplete_commands_are_not_rewritten(args):
    assert wrapper.normalize_parse_args(args) == args


def test_no_tier_adds_flash_before_end_of_options():
    args = ["parse", "-o", "output.md", "--", "-answer.docx"]
    assert wrapper.normalize_parse_args(args) == [
        "parse", "-o", "output.md", "--tier", "flash", "--", "-answer.docx",
    ]


def test_no_tier_adds_flash_after_existing_arguments():
    args = ["parse", "answer.docx", "--ocr-mode", "auto", "--output=output.md"]
    assert wrapper.normalize_parse_args(args) == [*args, "--tier", "flash"]


def test_mixed_short_and_long_options_only_rewrites_tier():
    args = [
        "parse", "-vooutput.docx", "-pall", "-fmarkdown", "--remote", "--disable-image-analysis",
        "--api-key=key.docx", "--ocr-mode", "auto", "--tier=standard", "answer.docx",
    ]
    expected = list(args)
    expected[-2] = "--tier=flash"
    expected.remove("-pall")
    assert wrapper.normalize_parse_args(args) == expected


def test_all_explicit_tiers_use_flash_for_single_docx():
    assert wrapper.normalize_parse_args([
        "parse", "--tier=standard", "answer.docx", "--tier", "advanced", "-o", "out.md",
    ]) == ["parse", "--tier=flash", "answer.docx", "--tier", "flash", "-o", "out.md"]


@pytest.mark.parametrize("option", [
    ["--pages", "all"], ["--pages=all"], ["-p", "all"], ["-pall"],
    ["--pages", "1-3"], ["--pages=1-3"], ["-p1-3"],
])
@pytest.mark.parametrize("extension", ["docx", "PNG", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif", "jp2"])
@pytest.mark.parametrize("tier", ["standard", "advanced"])
def test_single_docx_and_images_remove_pdf_only_page_option(option, extension, tier):
    args = ["parse", f"answer.{extension}", *option, "--tier", tier, "-o", "out.md"]
    expected_tier = "flash" if extension == "docx" else tier
    assert wrapper.normalize_parse_args(args) == ["parse", f"answer.{extension}", "--tier", expected_tier, "-o", "out.md"]


@pytest.mark.parametrize("option", [["-vpall"], ["-vvp", "all"]])
def test_removing_short_page_option_preserves_grouped_flags(option):
    args = ["parse", "answer.docx", *option, "-o", "out.md"]
    verbose = "-vv" if option[0] == "-vvp" else "-v"
    assert wrapper.normalize_parse_args(args) == ["parse", "answer.docx", verbose, "-o", "out.md", "--tier", "flash"]


def test_pdf_page_ranges_unchanged():
    args = ["parse", "answer.pdf", "--pages", "1-3", "--tier", "standard", "-o", "out.md"]
    assert wrapper.normalize_parse_args(args) == args


def test_removing_pages_keeps_delimiter_and_adds_tier_before_it():
    args = ["parse", "--pages=all", "-o", "out.md", "--", "-answer.docx"]
    assert wrapper.normalize_parse_args(args) == ["parse", "-o", "out.md", "--tier", "flash", "--", "-answer.docx"]
