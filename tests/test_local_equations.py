import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bb_assistant.local_equations import decode_legacy_equation


def test_decoder_uses_local_launcher_without_shell_and_keeps_output_private(tmp_path):
    def run(argv, **kwargs):
        assert argv[:3] == ["python-local.exe", "mineru_local.py", "decode-equation"]
        assert kwargs["shell"] is False
        assert Path(argv[3]).read_bytes() == b"formula payload"
        Path(argv[4]).write_text(json.dumps({"ok": True, "latex": r"\frac{1}{2}"}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)
    with patch("bb_assistant.local_equations.discover_local_command", return_value=["python-local.exe", "mineru_local.py"]), \
            patch("bb_assistant.local_equations.subprocess.run", side_effect=run) as command:
        assert decode_legacy_equation(b"formula payload", tmp_path) == r"\frac{1}{2}"
        assert decode_legacy_equation(b"formula payload", tmp_path) == r"\frac{1}{2}"
    command.assert_called_once()


@pytest.mark.parametrize("failure", [OSError("private-detail"), subprocess.TimeoutExpired([], 1)])
def test_failed_local_decoder_falls_back_without_leaking_details(tmp_path, failure):
    with patch("bb_assistant.local_equations.discover_local_command", return_value=["python", "wrapper"]), \
            patch("bb_assistant.local_equations.subprocess.run", side_effect=failure):
        assert decode_legacy_equation(b"valid-looking-data", tmp_path) is None


def test_no_local_decoder_uses_preview_fallback_without_launch(tmp_path):
    with patch("bb_assistant.local_equations.discover_local_command", return_value=None), \
            patch("bb_assistant.local_equations.subprocess.run") as command:
        assert decode_legacy_equation(b"data", tmp_path) is None
    command.assert_not_called()


def test_decoder_rejects_bad_output(tmp_path):
    def run(argv, **kwargs):
        Path(argv[4]).write_text(json.dumps({"ok": True, "latex": "bad\u0000formula"}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)
    with patch("bb_assistant.local_equations.discover_local_command", return_value=["python", "wrapper"]), \
            patch("bb_assistant.local_equations.subprocess.run", side_effect=run):
        assert decode_legacy_equation(b"data", tmp_path) is None
