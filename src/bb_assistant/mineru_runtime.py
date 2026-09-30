"""Find the app's local OCR installation without changing custom providers."""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path
from typing import Iterable


DEFAULT_MINERU_COMMAND = [
    "mineru-kit", "parse", "{input}", "-o", "{output}/document.md", "--tier", "advanced"
]
MINERU_TIERS = ("advanced", "standard", "basic", "flash")
MINERU_OCR_MODES = ("auto", "ocr", "txt")


def is_mineru_tier_command(command: object) -> bool:
    """Recognize only supported MinerU 4 parse launchers, not arbitrary OCR CLIs."""
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        return False
    if Path(command[0]).name.lower() in {"mineru-kit", "mineru-kit.exe"}:
        return len(command) > 1 and command[1] == "parse"
    return _is_local_wrapper(command) and len(command) > 2 and command[2] == "parse"


def is_local_mineru_wrapper(command: object) -> bool:
    """Whether the command uses the app's parse wrapper and its local helpers."""
    return is_mineru_tier_command(command) and _is_local_wrapper(command)


def _mineru_option_value(command: object, option: str, choices: tuple[str, ...]) -> str | None:
    if not is_mineru_tier_command(command):
        return None
    value = None
    for index, arg in enumerate(command):
        if arg == "--":
            break
        if arg == option:
            value = command[index + 1] if index + 1 < len(command) else None
        elif arg.startswith(option + "="):
            value = arg.partition("=")[2]
    return value if value in choices else None


def mineru_command_tier(command: object) -> str | None:
    """Read an explicit tier without guessing defaults or changing unknown values."""
    return _mineru_option_value(command, "--tier", MINERU_TIERS)


def mineru_command_ocr_mode(command: object) -> str | None:
    """Read the PDF text source independently from the parsing quality tier."""
    return _mineru_option_value(command, "--ocr-mode", MINERU_OCR_MODES)


def with_mineru_tier(command: list[str], tier: str) -> list[str]:
    """Change a recognized parse command only after an explicit tier selection."""
    if tier not in MINERU_TIERS:
        raise ValueError("不支持的本地 MinerU 识别档位。")
    return _with_mineru_option(command, "--tier", tier)


def with_mineru_ocr_mode(command: list[str], mode: str) -> list[str]:
    """Set a PDF text source only on a recognized MinerU 4 parse launcher."""
    if mode not in MINERU_OCR_MODES:
        raise ValueError("不支持的本地 MinerU PDF 文字识别方式。")
    return _with_mineru_option(command, "--ocr-mode", mode)


def _with_mineru_option(command: list[str], option: str, value: str) -> list[str]:
    if not is_mineru_tier_command(command):
        raise ValueError("自定义命令未识别为 MinerU 4；请直接编辑命令参数。")
    result = []
    index = 0
    while index < len(command):
        arg = command[index]
        if arg == "--":
            return result + [option, value] + command[index:]
        if arg == option:
            index += 1
            if index < len(command) and not command[index].startswith("--"):
                index += 1
            continue
        if arg.startswith(option + "="):
            index += 1
            continue
        result.append(arg)
        index += 1
    return result + [option, value]


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
            "--tier", "advanced", "--pages", "all", "--ocr-mode", "auto",
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
        # Repairing a relocated installation must keep an explicitly chosen tier.
        tier = mineru_command_tier(command)
        if tier is not None and tier != mineru_command_tier(local):
            local = with_mineru_tier(local, tier)
        ocr_mode = mineru_command_ocr_mode(command)
        if ocr_mode is not None and ocr_mode != mineru_command_ocr_mode(local):
            local = with_mineru_ocr_mode(local, ocr_mode)
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
