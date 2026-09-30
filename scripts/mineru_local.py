"""Run the project's MinerU installation with local models and isolated caches."""
from __future__ import annotations

import os
import sys
from pathlib import Path


MAX_EQUATION_FILE_BYTES = 8 * 1024 * 1024
MAX_EQUATION_STREAM_BYTES = 1024 * 1024
MAX_EQUATION_LATEX_CHARS = 100_000


def _read_equation_native(data: bytes) -> bytes:
    """Read a small OLE stream as data; never activate the embedded object."""
    import io

    import olefile

    if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ValueError("Not an OLE compound file")
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        streams = ole.listdir(streams=True, storages=False)
        if len(streams) > 128 or ["Equation Native"] not in streams:
            raise ValueError("Unsupported embedded object")
        total = 0
        for stream in streams:
            size = ole.get_size(stream)
            if size < 0 or size > MAX_EQUATION_STREAM_BYTES:
                raise ValueError("Embedded stream exceeds limit")
            total += size
        if total > MAX_EQUATION_FILE_BYTES:
            raise ValueError("Embedded object exceeds limit")
        native = ole.openstream("Equation Native").read(MAX_EQUATION_STREAM_BYTES + 1)
        if len(native) > MAX_EQUATION_STREAM_BYTES:
            raise ValueError("Equation stream exceeds limit")
        return native


def _load_equation_codec():
    # Import only for this subcommand, in the isolated MinerU environment.
    from docvortex.analyzers.native.office.equation.mtef import _MtefReader, decode_equation_native
    from docvortex.analyzers.native.office.equation.mtef_v5 import _MtefV5Reader

    return decode_equation_native, {3: _MtefReader, 5: _MtefV5Reader}


def _decode_native_equation(native: bytes) -> str | None:
    """Reject partial or unsupported native formula decodes."""
    import struct
    import unicodedata

    if not 28 <= len(native) <= MAX_EQUATION_STREAM_BYTES:
        return None
    header_size, version, _, object_size = struct.unpack_from("<HIHI", native)
    end = header_size + object_size
    if (header_size < 28 or version != 0x0002_0000 or not object_size
            or end > len(native) or any(native[end:])):
        return None
    body = native[header_size:end]
    decoder, readers = _load_equation_codec()
    reader_type = readers.get(body[0])
    if reader_type is None:
        return None
    reader = reader_type(body)
    reader.parse()
    # A decoder can return at the first END record. Unknown nonzero data after
    # that record must not be silently discarded from a student's formula.
    if not 0 < reader.pos <= len(body) or any(body[reader.pos:]):
        return None
    latex = decoder(native)
    if (not isinstance(latex, str) or not latex.strip()
            or len(latex) > MAX_EQUATION_LATEX_CHARS
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in latex)):
        return None
    return latex.strip()


def run_decode_equation(input_path: str | Path, output_path: str | Path) -> int:
    """Write a bounded JSON result without printing formula data or errors.

    Unsupported input is an ordinary ``ok: false`` result so the desktop app
    can use the formula's preview image instead. No OCR models are loaded.
    """
    import contextlib
    import json
    import tempfile

    source, destination = Path(input_path), Path(output_path)
    result = {"ok": False, "latex": ""}
    try:
        if source.resolve() == destination.resolve():
            return 2
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                if source.is_file() and source.stat().st_size <= MAX_EQUATION_FILE_BYTES:
                    with source.open("rb") as handle:
                        data = handle.read(MAX_EQUATION_FILE_BYTES + 1)
                    if len(data) <= MAX_EQUATION_FILE_BYTES:
                        latex = _decode_native_equation(_read_equation_native(data))
                        if latex:
                            result = {"ok": True, "latex": latex}
    except Exception:
        # Missing optional decoder, malformed compound files and unsupported
        # formulas all fall back without exposing filenames or payloads.
        pass
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=".equation-", suffix=".json", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, ensure_ascii=False)
        os.replace(temporary, destination)
        return 0
    except OSError:
        return 2
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def normalize_parse_args(argv: list[str]) -> list[str]:
    """Normalize format-specific options for a single DOCX or raster input.

    MinerU 4 accepts Office files only with ``flash``. Its single-file CLI
    rejects the PDF/image ``advanced`` tier in the app's default command.
    DOCX and raster images always parse in full; even ``--pages all`` is
    rejected for these inputs, so PDF-only page options are removed.
    Match the parse command's option grammar so an output filename or an API
    option ending in .docx is never mistaken for an input document. Unknown or
    incomplete options are left to the CLI to diagnose without rewriting them.
    ``argv`` excludes the launcher filename, like ``sys.argv[1:]``.
    """
    args = list(argv)
    if not args or args[0] != "parse":
        return args
    value_options = {
        "--output", "--pages", "--format", "--tier", "--remote-url", "--api-key", "--ocr-mode",
    }
    flag_options = {"--verbose", "--remote", "--disable-image-analysis", "--help"}
    short_values = {"o", "p", "f"}
    inputs: list[str] = []
    tiers: list[tuple[int, bool]] = []
    page_replacements: dict[int, str | None] = {}
    separator: int | None = None
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--":
            separator = index
            inputs.extend(args[index + 1:])
            break
        if arg.startswith("--"):
            option, equal, _ = arg.partition("=")
            if option in value_options:
                option_index = index
                if equal:
                    if option == "--tier":
                        tiers.append((index, True))
                    elif option == "--pages":
                        page_replacements[index] = None
                else:
                    if index + 1 >= len(args) or args[index + 1].startswith("-"):
                        return args
                    index += 1
                    if option == "--tier":
                        tiers.append((index, False))
                    elif option == "--pages":
                        page_replacements[option_index] = None
                        page_replacements[index] = None
            elif option not in flag_options or equal:
                return args
        elif arg.startswith("-") and arg != "-":
            # Click supports attached short values (-oout.md) and grouped
            # boolean options before them (-voout.md).
            for offset, letter in enumerate(arg[1:], start=1):
                if letter in short_values:
                    option_index = index
                    if offset + 1 == len(arg):
                        if index + 1 >= len(args) or args[index + 1].startswith("-"):
                            return args
                        index += 1
                        if letter == "p":
                            page_replacements[index] = None
                    if letter == "p":
                        page_replacements[option_index] = arg[:offset] if offset > 1 else None
                    break
                if letter != "v":
                    return args
        else:
            inputs.append(arg)
        index += 1
    raster_extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".jp2"}
    if len(inputs) != 1:
        return args
    extension = Path(inputs[0]).suffix.lower()
    if extension != ".docx" and extension not in raster_extensions:
        return args
    if extension == ".docx" and tiers:
        for position, attached in tiers:
            args[position] = "--tier=flash" if attached else "flash"
    args = [
        page_replacements.get(position, value)
        for position, value in enumerate(args)
        if page_replacements.get(position, value) is not None
    ]
    if extension == ".docx" and not tiers:
        insert_at = args.index("--") if separator is not None else len(args)
        args[insert_at:insert_at] = ["--tier", "flash"]
    return args


def prepare_environment() -> Path:
    project = Path(__file__).resolve().parents[1]
    home = project / ".mineru"
    # Ignore unrelated MinerU services/settings inherited from the desktop process.
    for name in list(os.environ):
        if name.startswith("MINERU_"):
            del os.environ[name]
    locations = {
        "MINERU_HOME": home,
        "HF_HOME": home / "cache" / "huggingface",
        "HF_HUB_CACHE": home / "cache" / "huggingface" / "hub",
        "MODELSCOPE_CACHE": home / "cache" / "modelscope",
        "MODELSCOPE_HUB_CACHE": home / "cache" / "modelscope" / "hub",
        "XDG_CACHE_HOME": home / "cache",
        "TORCH_HOME": home / "cache" / "torch",
        "TEMP": home / "tmp",
        "TMP": home / "tmp",
    }
    for name, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ.update({
        "MINERU_CONFIG": str(home / "config.yaml"),
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_SMALL_BACKEND": "onnx",
        "MINERU_MODEL_VLM_ENGINE": "llama-cpp",
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    if os.name == "nt":
        import ctypes
        # A PyInstaller parent can pass its private DLL directory to subprocesses.
        # Use the OCR environment's DLLs, not the desktop app's bundled libraries.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        os.environ["PATH"] = os.pathsep.join(map(str, [project / ".mineru-venv" / "Scripts", windows / "System32", windows]))
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return home


if __name__ == "__main__":
    prepare_environment()
    if sys.argv[1:2] == ["decode-equation"]:
        if len(sys.argv) != 4:
            raise SystemExit(2)
        raise SystemExit(run_decode_equation(sys.argv[2], sys.argv[3]))
    sys.argv[1:] = normalize_parse_args(sys.argv[1:])
    from mineru.kit.main import main
    main()
