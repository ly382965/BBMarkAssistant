"""Find the app's local OCR installation without changing custom providers."""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path
from typing import Iterable


DEFAULT_MINERU_COMMAND = [
    "mineru-kit", "parse", "{input}", "-o", "{output}/document.md", "--tier", "standard"
]


def _candidate_roots() -> list[Path]:
    roots = []
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        roots.extend([executable_dir, *list(executable_dir.parents)[:3]])
    # In a source checkout this is the project; frozen paths stay confined to
    # the bundle, so they cannot cause a scan of unrelated system directories.
    roots.append(Path(__file__).resolve().parents[2])
    return list(dict.fromkeys(roots))


def discover_local_command(search_roots: Iterable[Path] | None = None) -> list[str] | None:
    """Return a verified project-local launcher, checking only known layouts."""
    roots = _candidate_roots() if search_roots is None else search_roots
    for candidate in roots:
        root = Path(candidate).resolve()
        executable = root / ".mineru-venv" / "Scripts" / "python.exe"
        wrapper = root / "scripts" / "mineru_local.py"
        if not all(path.is_file() for path in (executable, wrapper, root / ".mineru" / "config.yaml")):
            continue
        return [
            str(executable), str(wrapper), "parse", "{input}", "-o", "{output}/document.md",
            "--tier", "standard", "--pages", "all", "--ocr-mode", "auto",
        ]
    return None


def _executable_exists(value: str) -> bool:
    # Relative paths would be interpreted from the per-document output folder
    # by subprocess, not from the application folder. Require absolute paths.
    if Path(value).is_absolute():
        return Path(value).is_file()
    if "/" in value or "\\" in value:
        return False
    return shutil.which(value) is not None


def _is_local_wrapper(command: list[str]) -> bool:
    return len(command) > 1 and Path(command[1]).name.lower() == "mineru_local.py"


def _wrapper_exists(command: list[str]) -> bool:
    return Path(command[1]).is_absolute() and Path(command[1]).is_file()


def resolve_ocr_config(config: dict, *, search_roots: Iterable[Path] | None = None) -> dict:
    """Repair missing stock launchers; retain working/custom/API configuration.

    No installation, network access, config writes or model loading occurs.
    Callers can use the returned copy both for settings and for execution.
    """
    result = copy.deepcopy(config)
    if result.get("mode", "command") != "command":
        return result
    command = result.get("command", DEFAULT_MINERU_COMMAND)
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        return result
    wrapper = _is_local_wrapper(command)
    if _executable_exists(command[0]) and (not wrapper or _wrapper_exists(command)):
        return result
    name = Path(command[0]).name.lower()
    if name not in {"mineru", "mineru.exe", "mineru-kit", "mineru-kit.exe"} and not wrapper:
        return result
    local = discover_local_command(search_roots)
    if local is not None:
        result["command"] = local
    return result


def validate_ocr_command(config: dict, *, search_roots: Iterable[Path] | None = None) -> list[str]:
    """Validate local launchability before a batch; leave API modes untouched."""
    resolved = resolve_ocr_config(config, search_roots=search_roots)
    if resolved.get("mode", "command") != "command":
        return []
    command = resolved.get("command", DEFAULT_MINERU_COMMAND)
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError("OCR command 必须是参数字符串数组，不能是拼接的 shell 命令。")
    if "{input}" in command[0] or "{output}" in command[0]:
        raise ValueError("OCR 可执行程序必须是固定命令，不能由作业文件路径决定。")
    if not any("{input}" in arg for arg in command[1:]) or not any("{output}" in arg for arg in command[1:]):
        raise ValueError("OCR command 必须包含 {input} 和 {output} 参数占位符。")
    if not _executable_exists(command[0]):
        raise ValueError("本地 OCR 可执行文件不存在；请检查连接设置中的可执行文件绝对路径。使用默认 MinerU 时，可运行 setup_mineru.ps1 安装。")
    if _is_local_wrapper(command) and not _wrapper_exists(command):
        raise ValueError("本地 MinerU 启动脚本不存在；请使用完整安装目录，或运行 setup_mineru.ps1 修复。")
    return command.copy()
