"""Decode legacy formula data with the installed local parser, never OLE activation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .mineru_runtime import discover_local_command


def decode_legacy_equation(payload: bytes, output: Path, timeout: float = 60) -> str | None:
    """Return checked LaTeX, or let the caller use the equation's cached preview.

    Input/output files stay in the private OCR run directory. No API keys or
    student text are passed through a shell, stdout, or a remote service.
    """
    if not payload or len(payload) > 8 * 1024 * 1024:
        return None
    launcher = discover_local_command()
    if launcher is None:
        return None
    output = Path(output)
    digest = hashlib.sha256(payload).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    source = output / f"{digest}.bin"
    result_path = output / f"{digest}.json"
    if not source.exists():
        source.write_bytes(payload)
    try:
        if not result_path.exists():
            result = subprocess.run(
                launcher[:2] + ["decode-equation", str(source), str(result_path)],
                shell=False, capture_output=True, check=False, timeout=min(max(timeout, 1), 60),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                return None
        if not result_path.is_file() or result_path.stat().st_size > 1024 * 1024:
            return None
        result = json.loads(result_path.read_text(encoding="utf-8"))
        latex = result.get("latex") if isinstance(result, dict) and result.get("ok") is True else None
        if isinstance(latex, str) and 0 < len(latex.strip()) <= 100000 and not any(
            ord(char) < 32 and char not in "\n\r\t" for char in latex
        ):
            return latex.strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None
