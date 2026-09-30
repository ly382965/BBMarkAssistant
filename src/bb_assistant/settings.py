from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_CREDENTIAL_PARAMETERS = {
    "apikey", "xapikey", "key", "password", "passwd", "pass", "authorization", "auth",
    "token", "accesstoken", "refreshtoken", "idtoken", "secret", "clientsecret",
    "cookie", "cookies", "ticket", "code", "sessionid", "jsessionid", "jwt", "bearer",
    "signature", "xamzsignature", "xamzcredential",
}


def _validate_connection_url(value: str, label: str, *, https_only=False, optional=False):
    """Validate connection fields before serializing; never inspect homework text."""
    if optional and not value:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是完整 URL。")
    try:
        parsed = urlparse(value)
        allowed_schemes = {"https"} if https_only else {"http", "https"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            raise ValueError(f"{label} 必须是完整 {'HTTPS' if https_only else 'HTTP/HTTPS'} URL。")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{label} 不能包含用户名或密码；凭据请填写在独立输入框。")
        _ = parsed.port
        # OAuth-style fragments can contain secrets just like query strings.
        for component in (parsed.query, parsed.fragment):
            for name in parse_qs(component, keep_blank_values=True):
                normalized = "".join(char for char in name.lower() if char.isalnum())
                if normalized in _CREDENTIAL_PARAMETERS:
                    raise ValueError(f"{label} 不能包含凭据查询参数；API Key 请填写在独立输入框。")
    except ValueError as exc:
        # urllib's errors may echo URL fragments; use a safe validation message.
        if str(exc).startswith(label):
            raise
        raise ValueError(f"{label} URL 格式无效。") from exc

# Example course only. Paste the actual course URL in connection settings.
DEFAULT_COURSE_URL = "https://www.bb.ustc.edu.cn/webapps/blackboard/execute/modulepage/view?course_id=_12345_1"
DEFAULTS = {
    "course_url": DEFAULT_COURSE_URL,
    "username": "",
    "bb": {"base_url": "https://www.bb.ustc.edu.cn", "course_id": "_12345_1", "timeout": 60},
    "ocr": {
        "mode": "command",
        "command": ["mineru-kit", "parse", "{input}", "-o", "{output}/document.md", "--tier", "advanced"],
        "endpoint": "http://127.0.0.1:8000/file_parse",
        "timeout": 900,
        "backend": "pipeline",
        "extra_params": {},
    },
    "grading": {
        "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "wire_api": "chat", "temperature": 0,
        "max_tokens": 16384, "timeout": 180, "max_input_chars": 100000, "extra_body": {},
    },
    "gpt": {
        "base_url": "https://api.openai.com/v1", "model": "gpt-5.6-sol", "wire_api": "responses",
        "reasoning_effort": "high", "max_tokens": 8192, "timeout": 180,
        "max_input_chars": 100000, "extra_body": {}, "http_headers": {},
    },
    "recognition": {"mode": "auto", "render_dpi": 200, "max_page_edge": 2200, "max_pages": 60},
    "actor_critic": {"max_revisions": 1},
    "rubric": {"max_score": 10, "instructions": "", "reference_answer": ""},
}


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BBMarkAssistant"
    return Path(__file__).resolve().parents[2] / "data"


def merge_config(base: dict, incoming: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in incoming.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


class Settings:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            self.data = merge_config(self.data, json.loads(self.path.read_text(encoding="utf-8")))
        from .mineru_runtime import resolve_ocr_config
        resolved = resolve_ocr_config(self.data["ocr"])
        self.ocr_repaired = resolved != self.data["ocr"]
        self.data["ocr"] = resolved
        self._secrets: dict[str, str] = {}

    def save(self, data: dict):
        from .mineru_runtime import resolve_ocr_config
        data = merge_config(DEFAULTS, data)
        # The installer can finish while this window still has the old command.
        # Resolve again on every save so those stale widgets cannot undo setup.
        data["ocr"] = resolve_ocr_config(data["ocr"])
        # Keys belong in the credential vault, never in the config JSON.
        def check(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k.lower().replace("-", "_") in {"api_key", "password", "authorization", "token", "secret", "access_token", "refresh_token", "cookie", "cookies", "client_secret"}:
                        raise ValueError("密码和 API Key 请填写在凭据输入框，不要放入高级配置。")
                    check(v)
            elif isinstance(value, list):
                for v in value:
                    check(v)
        check(data)
        _validate_connection_url(data["course_url"], "BB 课程地址", https_only=True)
        _validate_connection_url(data["bb"]["base_url"], "BB 服务地址", https_only=True)
        _validate_connection_url(data["bb"].get("cas_service", ""), "CAS service", optional=True)
        _validate_connection_url(data["ocr"]["endpoint"], "OCR API 地址")
        _validate_connection_url(data["grading"]["base_url"], "评分 API 地址")
        _validate_connection_url(data["gpt"]["base_url"], "GPT API 地址")
        if data["recognition"].get("mode") not in {"auto", "ocr", "vision"}:
            raise ValueError("作业识别路线必须为 auto、ocr 或 vision。")
        collaboration = data.get("actor_critic")
        revisions = collaboration.get("max_revisions") if isinstance(collaboration, dict) else None
        if type(revisions) is not int or not 0 <= revisions <= 2:
            raise ValueError("DS / GPT 协作最大修订次数必须为 0 到 2 的整数。")
        for provider in ("grading", "gpt"):
            if data[provider].get("wire_api", "chat") not in {"chat", "responses"}:
                raise ValueError("评分 API 协议必须为 chat 或 responses。")
            headers = data[provider].get("http_headers", {})
            if not isinstance(headers, dict):
                raise ValueError("http_headers 必须为 JSON 对象。")
            for name, value in headers.items():
                normalized = "".join(char for char in name.lower() if char.isalnum())
                if normalized in _CREDENTIAL_PARAMETERS or normalized in {"host", "contentlength"}:
                    raise ValueError("http_headers 不能包含凭据或覆盖 Host / Content-Length。")
                if not isinstance(value, str) or any(c in name + value for c in "\r\n"):
                    raise ValueError("http_headers 包含无效 HTTP 头。")
        parsed = urlparse(data["course_url"])
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("BB 课程地址必须是完整 HTTPS URL。")
        course = parse_qs(parsed.query).get("course_id", [""])[0]
        if not course:
            raise ValueError("课程 URL 中缺少 course_id。")
        data["bb"]["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
        data["bb"]["course_id"] = course
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.data = data

    def secret(self, name: str, *, include_environment: bool = True) -> str:
        # Provider-specific names prevent accidentally taking Codex's API key.
        variable = {"gpt": "BBMARK_GPT_API_KEY", "deepseek": "BBMARK_DEEPSEEK_API_KEY"}.get(name)
        if include_environment and variable and os.environ.get(variable, "").strip():
            return os.environ[variable].strip()
        if name in self._secrets:
            return self._secrets[name]
        try:
            import keyring
            return keyring.get_password("BBMarkAssistant:" + str(self.root.resolve()), name) or ""
        except Exception:
            return ""

    def set_secret(self, name: str, value: str, remember: bool):
        self._secrets[name] = value
        import keyring
        service = "BBMarkAssistant:" + str(self.root.resolve())
        if remember and value:
            keyring.set_password(service, name, value)
        else:
            try:
                keyring.delete_password(service, name)
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.NoKeyringError:
                if remember:
                    raise
                # No vault is available: the explicitly nonpersistent setting
                # remains usable in memory. Other vault failures stay visible.
