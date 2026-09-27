import importlib.util
import io
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "mineru_equation_wrapper", Path(__file__).resolve().parents[1] / "scripts" / "mineru_local.py"
)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def native(body=b"\x03\x01\x01\x03\x0a\x01\x00", **changes):
    header = {"header_size": 28, "version": 0x0002_0000, "object_size": len(body)}
    header.update(changes)
    return struct.pack("<HIHI", header["header_size"], header["version"], 0, header["object_size"]) + b"\x00" * 16 + body


def codec(monkeypatch, latex=r"\frac{1}{2}", consumed=None):
    class Reader:
        def __init__(self, data):
            self.pos = len(data) if consumed is None else consumed

        def parse(self):
            return None

    monkeypatch.setattr(wrapper, "_load_equation_codec", lambda: (lambda _: latex, {3: Reader, 5: Reader}))


def test_native_decode_requires_complete_supported_formula(monkeypatch):
    codec(monkeypatch)
    assert wrapper._decode_native_equation(native()) == r"\frac{1}{2}"
    codec(monkeypatch, consumed=5)
    assert wrapper._decode_native_equation(native()) is None
    assert wrapper._decode_native_equation(native(b"\x03\x01\x01\x03\x0a\x00\x00")) == r"\frac{1}{2}"


@pytest.mark.parametrize("data", [
    b"", b"\x00" * 27,
    native(header_size=27), native(header_size=100), native(version=0),
    native(object_size=0), native(object_size=100), native() + b"\x01",
    native(b"\x04\x01\x01\x03\x0a\x01\x00"),
])
def test_invalid_native_headers_fail_closed(monkeypatch, data):
    codec(monkeypatch)
    assert wrapper._decode_native_equation(data) is None


@pytest.mark.parametrize("latex", [
    None, "", "   ", 12, "x\x00y", "x\ny", "x\u200by", "x\ud800y",
    "x" * (wrapper.MAX_EQUATION_LATEX_CHARS + 1),
], ids=["none", "empty", "blank", "nontext", "nul", "newline", "invisible", "surrogate", "oversized"])
def test_invalid_decoded_text_is_rejected(monkeypatch, latex):
    codec(monkeypatch, latex=latex)
    assert wrapper._decode_native_equation(native()) is None


@pytest.mark.parametrize("consumed", [0, -1, 100])
def test_invalid_decoder_cursor_is_rejected(monkeypatch, consumed):
    codec(monkeypatch, consumed=consumed)
    assert wrapper._decode_native_equation(native()) is None


def fake_ole(monkeypatch, streams=None, size=20, payload=b"native"):
    class Compound:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def listdir(self, **_):
            return streams if streams is not None else [["Equation Native"]]

        def get_size(self, _):
            return size

        def openstream(self, _):
            return io.BytesIO(payload)

    monkeypatch.setitem(sys.modules, "olefile", SimpleNamespace(OleFileIO=lambda _: Compound()))


def test_ole_requires_equation_stream_and_checks_bounds(monkeypatch):
    magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    fake_ole(monkeypatch)
    assert wrapper._read_equation_native(magic) == b"native"
    with pytest.raises(ValueError):
        wrapper._read_equation_native(b"not OLE")
    fake_ole(monkeypatch, streams=[["Package"]])
    with pytest.raises(ValueError):
        wrapper._read_equation_native(magic)
    fake_ole(monkeypatch, size=wrapper.MAX_EQUATION_STREAM_BYTES + 1)
    with pytest.raises(ValueError):
        wrapper._read_equation_native(magic)


def test_noisy_decoder_never_prints_and_json_is_complete(monkeypatch, tmp_path, capsys):
    source, output = tmp_path / "input.bin", tmp_path / "result.json"
    source.write_bytes(b"test")
    monkeypatch.setattr(wrapper, "_read_equation_native", lambda _: b"native")

    def decode(_):
        print("private formula")
        print("private error", file=sys.stderr)
        return r"\frac{1}{2}"

    monkeypatch.setattr(wrapper, "_decode_native_equation", decode)
    assert wrapper.run_decode_equation(source, output) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True, "latex": r"\frac{1}{2}"}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert source.read_bytes() == b"test"
    assert not list(tmp_path.glob(".equation-*"))


@pytest.mark.parametrize("failure", [ValueError, ImportError, RuntimeError])
def test_unsupported_input_is_successful_false_result(monkeypatch, tmp_path, failure):
    source, output = tmp_path / "input.bin", tmp_path / "result.json"
    source.write_bytes(b"test")

    def read(_):
        raise failure("private filename or payload")

    monkeypatch.setattr(wrapper, "_read_equation_native", read)
    assert wrapper.run_decode_equation(source, output) == 0
    assert json.loads(output.read_text()) == {"ok": False, "latex": ""}


def test_missing_and_oversized_files_return_false_without_decoder(monkeypatch, tmp_path):
    source, output = tmp_path / "input.bin", tmp_path / "result.json"
    monkeypatch.setattr(wrapper, "_read_equation_native", lambda _: pytest.fail("decoder must not run"))
    assert wrapper.run_decode_equation(source, output) == 0
    assert json.loads(output.read_text()) == {"ok": False, "latex": ""}
    monkeypatch.setattr(wrapper, "MAX_EQUATION_FILE_BYTES", 3)
    source.write_bytes(b"1234")
    assert wrapper.run_decode_equation(source, output) == 0
    assert json.loads(output.read_text()) == {"ok": False, "latex": ""}


def test_input_is_never_overwritten_and_missing_output_parent_has_no_traceback(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"test")
    assert wrapper.run_decode_equation(source, source) == 2
    assert source.read_bytes() == b"test"
    assert wrapper.run_decode_equation(source, tmp_path / "missing" / "result.json") == 2
