"""Blackboard Original / Learn adapters, with an in-memory pyustc CAS session.

The Original protocol is based on the observed USTC public login service and
the protocol documented by the authors of https://github.com/Mortal/bbfetch
(blackboard/backend.py and blackboard/dwr.py). This is an independent adapter,
not a copy of that implementation. REST paths follow Blackboard's public API.
Original installations vary: unfamiliar response/form shapes fail explicitly.
The USTC inline-grader variant was verified against the authenticated page and
its official submission script; tests retain only synthetic protocol fixtures.
"""

from __future__ import annotations

import ast
import asyncio
import html
import math
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .attempts import latest_attempts


class BlackboardError(RuntimeError):
    """An actionable integration error safe to display without credentials."""


class GradeVerificationError(BlackboardError):
    """A write may have succeeded; do not retry until checking the live grade."""


class GradeUploadBlockedError(BlackboardError):
    """Preparation failed before any grade write was sent; review is retained."""


@dataclass(frozen=True)
class Student:
    student_id: str
    name: str
    bb_user_id: str


@dataclass(frozen=True)
class Assignment:
    id: str
    title: str
    max_score: float


@dataclass(frozen=True)
class Attempt:
    id: str
    student_id: str
    assignment_id: str
    detail_url: str
    submitted_at: str = ""
    status: str = "submitted"


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str


DEFAULT_ENDPOINTS = {
    "login_page": "/webapps/login/",
    "grade_center": "/webapps/gradebook/do/instructor/enterGradeCenter?course_id={course_id}&cvid=fullGC",
    "overview": "/webapps/gradebook/do/instructor/getJSONData?course_id={course_id}",
    "dwr_engine": "/javascript/dwr/engine.js",
    "attempts_dwr": "/webapps/gradebook/dwr/call/plaincall/GradebookDWRFacade.getAttemptsInfo.dwr",
    "attempt": "/webapps/assignment/gradeAssignmentRedirector?course_id={course_id}&attempt_id={attempt_id}",
    "rest_students": "/learn/api/public/v1/courses/{course_id}/users?expand=user&limit=200",
    "rest_user": "/learn/api/public/v1/users/{user_id}?fields=id,userName,studentId,name",
    "rest_assignments": "/learn/api/public/v2/courses/{course_id}/gradebook/columns?limit=200",
    "rest_attempts": "/learn/api/public/v2/courses/{course_id}/gradebook/columns/{assignment_id}/attempts?limit=200",
    "rest_attempt": "/learn/api/public/v2/courses/{course_id}/gradebook/columns/{assignment_id}/attempts/{attempt_id}",
    "rest_files": "/learn/api/public/v1/courses/{course_id}/gradebook/attempts/{attempt_id}/files?limit=200",
    "rest_download": "/learn/api/public/v1/courses/{course_id}/gradebook/attempts/{attempt_id}/files/{file_id}/download",
}


def _text(value: Any) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def _student_number(*values: Any) -> str:
    for value in values:
        candidate = _text(value).strip().upper()
        if re.fullmatch(r"[A-Z]{2}\d{8}", candidate):
            return candidate
    # All enrolments must remain visible even if their institutional ID is unusual.
    return next((_text(v).strip().upper() for v in values if _text(v).strip()), "")


def safe_filename(name: str) -> str:
    """Keep attachments inside their attempt folder, including on Windows."""
    name = unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        name = "attachment.bin"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        name = "_" + name
    if len(name) > 180:
        suffix = Path(name).suffix[:20]
        name = name[: 180 - len(suffix)] + suffix
    return name


def _js_value(source: str, variables: dict[str, Any]) -> Any:
    """Parse only DWR data literals/references; never execute JavaScript/Python."""
    def convert(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in {"null", "true", "false"}:
                return {"null": None, "true": True, "false": False}[node.id]
            if re.fullmatch(r"s\d+", node.id) and node.id in variables:
                return variables[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [convert(element) for element in node.elts]
        if isinstance(node, ast.Dict):
            return {convert(k): convert(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = convert(node.operand)
            if isinstance(value, (int, float)):
                return -value
        raise BlackboardError("BB DWR 返回了不支持的数据表达式；请检查服务版本。")

    try:
        return convert(ast.parse(source.strip(), mode="eval").body)
    except (SyntaxError, KeyError, TypeError, ValueError) as exc:
        raise BlackboardError("BB DWR 数据格式无法解析；没有执行返回的脚本。") from exc


def parse_dwr_attempts(source: str) -> dict[int, list[dict[str, Any]]]:
    """Decode the narrow DWR callback grammar used by getAttemptsInfo."""
    if "_remoteHandleException" in source or "_remoteHandleBatchException" in source:
        raise BlackboardError("BB 拒绝读取提交记录。请重新登录并检查成绩中心权限。")
    # Split statements without splitting semicolons inside quoted strings.
    tokens = re.findall(r'''(?:[^;'"/]|/(?!/)|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')+|//[^\n]*''', source)
    variables: dict[str, Any] = {}
    results: dict[int, list[dict[str, Any]]] = {}
    for token in tokens:
        token = token.strip()
        if not token or token.startswith("//"):
            continue
        if re.fullmatch(r"throw\s+['\"]allowScriptTagRemoting is false\.['\"]", token):
            continue
        declaration = re.fullmatch(r"var\s+(s\d+)\s*=\s*(.*)", token, re.S)
        if declaration:
            variables[declaration[1]] = _js_value(declaration[2], variables)
            continue
        assignment = re.fullmatch(r"(s\d+)\.([A-Za-z_]\w*)\s*=\s*(.*)", token, re.S)
        if assignment:
            target = variables.get(assignment[1])
            if not isinstance(target, dict):
                raise BlackboardError("BB DWR 对象结构异常。")
            target[assignment[2]] = _js_value(assignment[3], variables)
            continue
        index = re.fullmatch(r"(s\d+)\[(\d+)\]\s*=\s*(.*)", token, re.S)
        if index:
            target = variables.get(index[1])
            i = int(index[2])
            if not isinstance(target, list) or i > 10000:
                raise BlackboardError("BB DWR 列表结构异常。")
            target.extend([None] * max(0, i + 1 - len(target)))
            target[i] = _js_value(index[3], variables)
            continue
        callback = re.fullmatch(
            r"dwr\.engine\._remoteHandleCallback\(\s*['\"]\d+['\"]\s*,\s*['\"](\d+)['\"]\s*,(.*)\)",
            token, re.S,
        )
        if callback:
            data = _js_value(callback[2], variables)
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise BlackboardError("BB 提交列表结构异常。")
            results[int(callback[1])] = data
            continue
        raise BlackboardError("BB DWR 返回了未知语句；已停止解析，未执行脚本。")
    if not results:
        raise BlackboardError("BB 没有返回提交列表回调。")
    return results


class BlackboardClient:
    """Blocking API intended for a GUI worker, never the UI thread.

    `mode` is auto/legacy/rest. Auto first uses the Original grade center found
    at USTC, then REST only if Original is unavailable. REST generally needs an
    institution-approved OAuth bearer token (`access_token`). Endpoint overrides
    are same-origin URL templates using the keys in DEFAULT_ENDPOINTS.
    """

    def __init__(
        self, base_url: str, course_id: str, timeout: int = 60, *,
        mode: str = "auto", endpoints: dict[str, str] | None = None,
        access_token: str = "", cas_service: str = "",
        session: requests.Session | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("BB 地址必须是 HTTPS 网站地址，不能含用户名或密码。")
        if mode not in {"auto", "legacy", "rest"}:
            raise ValueError("BB 模式必须是 auto、legacy 或 rest。")
        if not re.fullmatch(r"_\d+_\d+", course_id):
            raise ValueError("课程 ID 应为 _12345_1 这样的 Blackboard ID；请从实际课程 URL 复制。")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.course_id = course_id
        self.timeout = timeout
        self.mode = mode
        self.endpoints = {**DEFAULT_ENDPOINTS, **(endpoints or {})}
        self.cas_service = cas_service
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "BB-MarkAssistant/0.1", "Accept": "*/*"})
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"
        self._active_mode: str | None = mode if mode != "auto" else None
        self._overview_cache: dict[str, Any] | None = None
        self._students: dict[str, Student] = {}
        self._assignments: dict[str, Assignment] = {}
        self._script_session_id = ""

    def close(self) -> None:
        self.session.close()
        self.session.cookies.clear()

    def _url(self, path: str) -> str:
        result = urljoin(self.base_url + "/", path)
        current, base = urlsplit(result), urlsplit(self.base_url)
        if current.scheme != "https" or current.netloc.lower() != base.netloc.lower():
            raise BlackboardError("BB 返回了跨站或不安全的链接；未向该地址发送会话。")
        if current.username or current.password:
            raise BlackboardError("BB 链接不能包含登录凭据。")
        return result

    def _endpoint(self, key: str, **params: str) -> str:
        values = {"course_id": self.course_id, **params}
        return self._url(self.endpoints[key].format(**{k: quote(str(v), safe="") for k, v in values.items()}))

    def _request(self, method: str, url: str, *, allow_login: bool = False, **kwargs: Any) -> requests.Response:
        """Follow only same-origin HTTPS redirects, with no automatic write retry."""
        current = self._url(url)
        for _ in range(10):
            try:
                response = self.session.request(
                    method, current, timeout=self.timeout, allow_redirects=False, **kwargs,
                )
            except requests.RequestException as exc:
                # Exception text may contain signed URLs, so do not surface it.
                raise BlackboardError("BB 请求失败或超时，请检查网络和登录状态。") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                target = urljoin(current, response.headers.get("Location", ""))
                part = urlsplit(target)
                if allow_login and part.netloc != urlsplit(self.base_url).netloc:
                    return response
                # USTC's public CAS service embeds a legacy HTTP return URL.
                # Upgrade it before any request, keeping cookies on HTTPS only.
                if part.scheme == "http" and part.netloc == urlsplit(self.base_url).netloc:
                    target = urlunsplit(("https", part.netloc, part.path, part.query, part.fragment))
                current = self._url(target)
                if response.status_code == 303 or (method != "GET" and response.status_code in {301, 302}):
                    method, kwargs = "GET", {}
                elif method not in {"GET", "HEAD"}:
                    raise BlackboardError("BB 要求重定向写入请求；为避免重复提交，已停止。")
                continue
            if response.status_code in {401, 403}:
                raise BlackboardError("BB 登录已失效或缺少成绩中心权限（HTTP 401/403）。")
            if response.status_code >= 400:
                raise BlackboardError(f"BB 接口返回 HTTP {response.status_code}：{urlsplit(current).path}")
            if not kwargs.get("stream"):
                response.encoding = response.apparent_encoding or "utf-8"
                if not allow_login and self._is_login_page(response.text):
                    raise BlackboardError("BB 返回登录页，请重新通过统一身份认证登录。")
            return response
        raise BlackboardError("BB 重定向次数过多，请重新登录。")

    @staticmethod
    def _is_login_page(source: str) -> bool:
        soup = BeautifulSoup(source, "html.parser")
        return bool(soup.select_one('input[type="password"]') or soup.select_one('form#login'))

    def discover_cas_service(self) -> str:
        """Discover native SSO or USTC nginx_auth gateway through bounded hops."""
        pending, seen = [self._endpoint("login_page")], set()
        while pending and len(seen) < 10:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as exc:
                raise BlackboardError("无法读取 BB 统一身份认证入口。") from exc
            if response.status_code >= 400:
                raise BlackboardError("无法读取 BB 登录页；可在高级配置中设置 CAS service。")
            candidates = [response.headers.get("Location", "")]
            soup = BeautifulSoup(response.content, "html.parser")
            candidates.extend(str(link.get("href", "")) for link in soup.select("a[href]"))
            for candidate in candidates:
                if not candidate:
                    continue
                candidate = urljoin(url, html.unescape(candidate))
                parsed = urlsplit(candidate)
                service = parse_qs(parsed.query).get("service", [""])[0]
                if service and parsed.hostname in {"passport.ustc.edu.cn", "id.ustc.edu.cn"}:
                    return self._validate_cas_service(service)
                if parsed.netloc == urlsplit(self.base_url).netloc and (
                    candidate == urljoin(url, response.headers.get("Location", ""))
                    or re.match(r"/nginx_auth(?:/|$)", parsed.path)
                ):
                    pending.append(self._url(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))))
        raise BlackboardError("未发现 BB CAS service。请在高级配置中填写统一认证链接的 service 参数。")

    def _validate_cas_service(self, service: str) -> str:
        parsed = urlsplit(service)
        if (parsed.scheme not in {"https", "http"} or parsed.netloc != urlsplit(self.base_url).netloc
                or parsed.username or parsed.password):
            raise BlackboardError("CAS service 必须属于当前 BB 网站。")
        # CAS service is an exact identifier. Preserve legacy http here but redeem
        # its ticket exclusively over HTTPS in login().
        return service

    def login(self, username: str, password: str) -> None:
        if not username.strip() or not password:
            raise ValueError("请输入统一身份认证账号和密码。")
        service = self._validate_cas_service(self.cas_service) if self.cas_service else self.discover_cas_service()

        async def authenticate() -> None:
            from pyustc import CASClient

            async with CASClient.login_by_pwd(username.strip(), password) as cas:
                current_service = service
                used_services = set()
                for _ in range(3):
                    if current_service in used_services:
                        raise BlackboardError("统一身份认证回到了同一登录入口，请检查 BB 登录配置。")
                    used_services.add(current_service)
                    ticket = await cas.get_ticket(current_service)
                    parsed = urlsplit(current_service)
                    callback = self._url(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, "")))
                    separator = "&" if parsed.query else "?"
                    self._request("GET", callback + separator + urlencode({"ticket": ticket}), allow_login=True)
                    page = self._request("GET", self._endpoint("grade_center"), allow_login=True)
                    if (not page.is_redirect and not self._is_login_page(page.text)
                            and "/nginx_auth" not in urlsplit(page.url).path):
                        return
                    next_service = parse_qs(urlsplit(page.headers.get("Location", "")).query).get("service", [""])[0]
                    current_service = (self._validate_cas_service(next_service) if next_service
                                       else self.discover_cas_service())
                raise BlackboardError("已通过 CAS，但 BB 成绩中心仍未登录，请检查课程权限。")

        try:
            asyncio.run(asyncio.wait_for(authenticate(), timeout=self.timeout * 3))
        except BlackboardError:
            raise
        except Exception as exc:
            raise BlackboardError(
                "pyustc 统一身份认证失败。请检查账号、密码、网络；若需要验证码或二次认证，先在浏览器完成。"
            ) from exc
        self._overview_cache = None
        self._students.clear()
        self._assignments.clear()
        self._script_session_id = ""

    def _json(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise BlackboardError("BB 返回了非 JSON 内容；该版本可能不支持所选接口。") from exc

    def _overview(self, refresh: bool = False) -> dict[str, Any]:
        if self._overview_cache is not None and not refresh:
            return self._overview_cache
        payload = self._json(self._request("GET", self._endpoint("overview")))
        if isinstance(payload, dict):
            payload = payload.get("cachedBook", payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("colDefs"), list) or not isinstance(payload.get("rows"), list):
            raise BlackboardError("BB 成绩中心缺少 colDefs/rows；需要适配当前学校版本。")
        self._overview_cache = payload
        return payload

    def _select_mode(self) -> str:
        if self._active_mode:
            return self._active_mode
        try:
            self._overview()
            self._active_mode = "legacy"
        except BlackboardError:
            # A REST probe is read-only; a rejected legacy call never triggers a write.
            self._paged(self._endpoint("rest_assignments"))
            self._active_mode = "rest"
        return self._active_mode

    def _paged(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            url = self._url(url)
            if url in seen or len(seen) >= 1000:
                raise BlackboardError("BB 分页重复或超出上限，未将部分名单当成完整名单。")
            seen.add(url)
            payload = self._json(self._request("GET", url))
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise BlackboardError("BB REST 缺少 results；请检查 API 版本和授权。")
            if any(not isinstance(item, dict) for item in payload["results"]):
                raise BlackboardError("BB REST 列表内容格式错误。")
            results.extend(payload["results"])
            url = (payload.get("paging") or {}).get("nextPage", "")
        return results

    def list_students(self) -> list[Student]:
        students: list[Student] = []
        if self._select_mode() == "legacy":
            # A user-triggered sync must observe roster changes, not a login-time snapshot.
            self._assignments.clear()
            for row in self._overview(refresh=True)["rows"]:
                if not isinstance(row, list) or not row or not isinstance(row[0], dict):
                    raise BlackboardError("BB 学生行结构不受支持，已停止导入。")
                cells = {str(cell["c"]): cell.get("v", "") for cell in row if isinstance(cell, dict) and "c" in cell}
                student_id = _student_number(cells.get("SI"), cells.get("UN"))
                bb_id = str(row[0].get("uid", ""))
                name = "".join(_text(cells.get(key)) for key in ("LN", "FN"))
                if not student_id or not bb_id:
                    raise BlackboardError("BB 学生行缺少学号或用户 ID，不能可靠关联成绩。")
                students.append(Student(student_id, name or student_id, bb_id))
        else:
            for membership in self._paged(self._endpoint("rest_students")):
                if membership.get("courseRoleId") not in {None, "Student", "Learner"}:
                    continue
                bb_id = str(membership.get("userId", ""))
                user = membership.get("user")
                if not isinstance(user, dict):
                    user = self._json(self._request("GET", self._endpoint("rest_user", user_id=bb_id)))
                student_id = _student_number(user.get("studentId"), user.get("userName"))
                name_obj = user.get("name") or {}
                name = "".join(_text(name_obj.get(key)) for key in ("family", "given"))
                if not bb_id or not student_id:
                    raise BlackboardError("BB REST 名单缺少学号或用户 ID。")
                students.append(Student(student_id, name or student_id, bb_id))
        if len({item.student_id for item in students}) != len(students):
            raise BlackboardError("BB 名单含重复学号，无法安全关联成绩；请先核对名单。")
        self._students = {item.bb_user_id: item for item in students}
        return sorted(students, key=lambda item: item.student_id)

    def list_assignments(self) -> list[Assignment]:
        assignments: list[Assignment] = []
        if self._select_mode() == "legacy":
            columns = self._overview(refresh=True)["colDefs"]
            for column in columns:
                if column.get("src") != "resource/x-bb-assignment":
                    continue
                score = self._max_score(column.get("points"))
                assignments.append(Assignment(str(column["id"]), _text(column.get("name")), score))
        else:
            for column in self._paged(self._endpoint("rest_assignments")):
                if ((column.get("grading") or {}).get("type") != "Attempts"
                        or column.get("scoreProviderHandle") != "resource/x-bb-assignment"):
                    continue
                score = self._max_score((column.get("score") or {}).get("possible"))
                assignments.append(Assignment(str(column["id"]), _text(column.get("name")), score))
        self._assignments = {item.id: item for item in assignments}
        return assignments

    @staticmethod
    def _max_score(value: Any) -> float:
        try:
            score = float(value)
        except (ValueError, TypeError) as exc:
            raise BlackboardError("BB 作业缺少有效满分，不能自动推断满分。") from exc
        if not math.isfinite(score) or score <= 0:
            raise BlackboardError("BB 作业满分必须是正的有限数值。")
        return score

    def list_attempts(self, assignment_id: str) -> list[Attempt]:
        if not self._students:
            self.list_students()
        if not self._assignments:
            self.list_assignments()
        if assignment_id not in self._assignments:
            raise BlackboardError("选择的作业不在当前课程中，请刷新作业列表。")
        if self._select_mode() == "legacy":
            return self._legacy_attempts(assignment_id)
        attempts: list[Attempt] = []
        for data in self._paged(self._endpoint("rest_attempts", assignment_id=assignment_id)):
            if data.get("status") in {"InProgress", "InProgressAgain"}:
                continue
            if data.get("groupAttemptId"):
                raise BlackboardError("此作业存在小组提交，请在 BB 中人工处理小组成绩。")
            student = self._students.get(str(data.get("userId", "")))
            if student is None:
                raise BlackboardError("提交无法对应课程名单（可能为匿名评分）；请检查 BB 设置。")
            aid = str(data.get("id", ""))
            if not aid:
                raise BlackboardError("BB 提交缺少 attempt ID。")
            attempts.append(Attempt(
                aid, student.student_id, assignment_id,
                self._endpoint("rest_attempt", assignment_id=assignment_id, attempt_id=aid),
                str(data.get("attemptDate") or data.get("created") or ""), str(data.get("status", "submitted")),
            ))
        return attempts

    def _legacy_attempts(self, assignment_id: str, student_filter: str | None = None) -> list[Attempt]:
        column = next(c for c in self._overview()["colDefs"] if str(c.get("id")) == assignment_id)
        if column.get("groupActivity"):
            raise BlackboardError("此作业为小组作业；当前版本只自动处理个人作业。")
        # This page sets the gradebook-scoped JSESSIONID used by DWR.
        self._request("GET", self._endpoint("grade_center"))
        if not self._script_session_id:
            engine = self._request("GET", self._endpoint("dwr_engine")).text
            match = re.search(r'''_origScriptSessionId\s*=\s*["']([^"']+)["']''', engine)
            if not match:
                raise BlackboardError("无法取得 BB DWR scriptSessionId；请检查 BB 版本或 dwr_engine 地址。")
            self._script_session_id = match[1] + uuid.uuid4().hex[:8]
        sessions = [cookie for cookie in self.session.cookies if cookie.name == "JSESSIONID"]
        sessions.sort(key=lambda cookie: len(cookie.path or ""), reverse=True)
        session_id = next((cookie.value for cookie in sessions if "/webapps/gradebook".startswith(cookie.path or "/")), "")
        if not session_id:
            raise BlackboardError("BB 成绩中心没有建立 JSESSIONID，请重新登录。")
        students = [student for student in self._students.values()
                    if student_filter is None or student.student_id == student_filter]
        attempts: list[Attempt] = []
        for start in range(0, len(students), 20):
            batch = students[start:start + 20]
            payload: dict[str, Any] = {
                "callCount": len(batch), "page": urlsplit(self._endpoint("grade_center")).path + "?" + urlsplit(self._endpoint("grade_center")).query,
                "httpSessionId": session_id, "scriptSessionId": self._script_session_id,
                "batchId": start // 20,
            }
            for index, student in enumerate(batch):
                fields = {
                    "scriptName": "GradebookDWRFacade", "methodName": "getAttemptsInfo", "id": index,
                    "param0": "number:" + self.course_id.split("_")[1],
                    "param1": "string:" + student.bb_user_id, "param2": "string:" + assignment_id,
                }
                payload.update({f"c{index}-{key}": value for key, value in fields.items()})
            result = parse_dwr_attempts(self._request("POST", self._endpoint("attempts_dwr"), data=payload).text)
            if set(result) != set(range(len(batch))):
                raise BlackboardError("BB 返回的提交列表不完整；已停止，避免漏批。")
            for index, student in enumerate(batch):
                for data in result[index]:
                    if data.get("groupAttemptId"):
                        raise BlackboardError("发现小组提交，当前版本不能按个人自动上传该成绩。")
                    raw_status = str(data.get("status") or "")
                    if raw_status.lower() in {"ip", "inprogress", "in_progress", "inprogressagain"}:
                        continue
                    if raw_status not in {"", "ng", "nr"}:
                        raise BlackboardError("BB 返回了未识别的提交状态，需要适配当前版本。")
                    status = {"": "Completed", "ng": "NeedsGrading", "nr": "NeedsReconciliation"}[raw_status]
                    aid = str(data.get("id", ""))
                    if not re.fullmatch(r"_?\d+(?:_\d+)?", aid):
                        raise BlackboardError("BB 提交记录缺少可识别的 attempt ID。")
                    attempts.append(Attempt(
                        aid, student.student_id, assignment_id,
                        self._endpoint("attempt", attempt_id=aid), str(data.get("date") or ""), status,
                    ))
        return attempts

    def _attempt_url(self, attempt: Attempt) -> str:
        key = "rest_attempt" if self._select_mode() == "rest" else "attempt"
        # Reconstruct from IDs rather than trusting a persisted or externally supplied URL.
        return self._endpoint(key, assignment_id=attempt.assignment_id, attempt_id=attempt.id)

    def download_attempt(self, attempt: Attempt, destination: Path) -> list[Path]:
        detail = self._request("GET", self._attempt_url(attempt))
        attachments: list[Attachment] = []
        submission_text = ""
        if self._select_mode() == "rest":
            payload = self._json(detail)
            self._verify_rest_identity(payload, attempt)
            submission = BeautifulSoup(str(payload.get("studentSubmission") or ""), "html.parser")
            if submission.select_one("img, object, iframe, embed, svg, math"):
                raise BlackboardError("作业正文包含图片或嵌入内容，请先人工导出完整 PDF，避免遗漏答案。")
            submission_text = submission.get_text("\n", strip=True)
            for item in self._paged(self._endpoint("rest_files", attempt_id=attempt.id)):
                fid = str(item.get("id", ""))
                if not fid:
                    raise BlackboardError("BB 附件缺少文件 ID。")
                attachments.append(Attachment(
                    str(item.get("fileName") or item.get("name") or f"{fid}.bin"),
                    self._endpoint("rest_download", attempt_id=attempt.id, file_id=fid),
                ))
        else:
            soup = BeautifulSoup(detail.content, "html.parser")
            if not soup.select_one("#currentAttempt"):
                raise BlackboardError("BB 未返回可查看的提交详情；该提交可能尚未正式提交。")
            self._verify_legacy_identity(soup, attempt)
            text_node = soup.select_one("#submissionTextView")
            if text_node:
                if text_node.select_one("img, object, iframe, embed, svg, math"):
                    raise BlackboardError("作业正文包含图片或嵌入内容，请先人工导出完整 PDF，避免遗漏答案。")
                submission_text = text_node.get_text("\n", strip=True)
            for item in soup.select("#currentAttempt_submissionList > li"):
                if (not item.select_one("a.dwnldBtn")
                        and not (item.select_one("#currentAttempt_attemptFilesubmissionText") and submission_text)):
                    raise BlackboardError("有提交附件缺少可识别的下载链接，已停止，避免遗漏后评分。")
            for link in soup.select("#currentAttempt_submissionList a.dwnldBtn, #currentAttempt a.dwnldBtn"):
                href = str(link.get("href", ""))
                if not href:
                    continue
                query_name = parse_qs(urlsplit(href).query).get("fileName", [""])[0]
                name = query_name or str(link.get("download", "")) or link.get_text(strip=True)
                if not name or name in {"下载", "Download"}:
                    parent = link.find_parent("li")
                    candidate = parent.get_text(" ", strip=True) if parent else ""
                    candidate = re.sub(r"\s*(Download|下载)\s*$", "", candidate, flags=re.I)
                    name = candidate or unquote(urlsplit(href).path.rsplit("/", 1)[-1]) or "attachment.bin"
                attachments.append(Attachment(name, self._url(urljoin(detail.url, href))))
        if not attachments and not submission_text:
            raise BlackboardError("本次提交没有可下载的附件或正文，需人工检查。")
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        used: set[str] = set()

        def allocate(raw_name: str) -> str:
            original = safe_filename(raw_name)
            name, index = original, 2
            while name.casefold() in used:
                name = f"{index}_{original}"
                index += 1
            used.add(name.casefold())
            return name

        for attachment in dict.fromkeys(attachments):
            name = allocate(attachment.name)
            target = destination / name
            if target.resolve().parent != destination:
                raise BlackboardError("附件路径超出了下载目录。")
            response = self._request("GET", attachment.url, stream=True)
            temporary = target.with_name(target.name + ".part")
            try:
                size = 0
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        if size == 0 and (b"<html" in chunk[:500].lower() or b"<!doctype html" in chunk[:500].lower()):
                            if "html" not in target.suffix.lower():
                                raise BlackboardError("附件接口返回 HTML 页面，可能登录过期；未保存为 PDF。")
                        size += len(chunk)
                        if size > 512 * 1024 * 1024:
                            raise BlackboardError("附件超过 512 MB，请在 BB 中手动下载。")
                        stream.write(chunk)
                if not size:
                    raise BlackboardError("BB 返回空附件。")
                temporary.replace(target)
            except requests.RequestException as exc:
                raise BlackboardError("附件下载中断，请检查网络后重新下载本次提交。") from exc
            finally:
                response.close()
                temporary.unlink(missing_ok=True)
            paths.append(target)
        if submission_text:
            target = destination / allocate("submission_text.txt")
            target.write_text(submission_text, encoding="utf-8")
            paths.append(target)
        return paths

    def _verify_rest_identity(self, payload: dict[str, Any], attempt: Attempt) -> None:
        if not self._students:
            self.list_students()
        student = self._students.get(str(payload.get("userId", "")))
        if str(payload.get("id", "")) != attempt.id or not student or student.student_id != attempt.student_id:
            raise BlackboardError("BB 回读的提交 ID 或学号不一致，已停止操作。")
        if payload.get("groupAttemptId"):
            raise BlackboardError("此提交属于小组，当前版本不能自动上传小组成绩。")

    def _verify_legacy_identity(self, soup: Any, attempt: Attempt) -> None:
        fields = soup.select('#currentAttempt_form input[name="attempt_id"], '
                             '#currentAttempt_form input[name="attemptId"], '
                             '#currentAttempt_form input[name="currentAttemptId"]')
        if not fields or any(str(field.get("value", "")) != attempt.id for field in fields):
            raise BlackboardError("BB 详情页的提交 ID 与所选提交不一致，已停止操作。")
        course = soup.select_one('#currentAttempt_form input[name="course_id"]')
        if course and str(course.get("value", "")) != self.course_id:
            raise BlackboardError("BB 详情页的课程 ID 与当前课程不一致。")

    @staticmethod
    def _form_values(form: Any) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        for field in form.select("input[name], textarea[name], select[name]"):
            kind = str(field.get("type", "text")).lower()
            if field.has_attr("disabled") or kind in {"submit", "button", "reset", "file", "image"}:
                continue
            if kind in {"radio", "checkbox"} and not field.has_attr("checked"):
                continue
            if field.name == "select":
                options = field.select("option[selected]") or field.select("option")[:1]
                values.extend((str(field["name"]), str(option.get("value", option.get_text()))) for option in options)
            else:
                value = field.get_text() if field.name == "textarea" else str(field.get("value", ""))
                values.append((str(field["name"]), value))
        return values

    def _grade_form(self, response: requests.Response, attempt: Attempt) -> tuple[Any, list[tuple[str, str]]]:
        soup = BeautifulSoup(response.content, "html.parser")
        forms = soup.select("form#currentAttempt_form")
        if len(forms) != 1:
            raise BlackboardError("BB 未返回 currentAttempt_form，不能安全上传成绩。")
        form = forms[0]
        values = self._form_values(form)
        lookup = dict(values)
        identity_keys = {"attempt_id", "attemptId", "currentAttemptId", "course_id"}
        unique_keys = identity_keys | {"grade", "feedbacktext"} | {
            key for key, _ in values if re.search(r"nonce|csrf|token", key, re.I)
        }
        if any(sum(name == key for name, _ in values) > 1 for key in unique_keys):
            raise BlackboardError("BB 评分表单含重复的身份、成绩或安全字段，已阻止写入。")
        ids = [value for key, value in values if key in identity_keys - {"course_id"}]
        if not ids or any(value != attempt.id for value in ids):
            raise BlackboardError("BB 评分表单缺少匹配的提交 ID，已阻止写入。")
        if "course_id" in lookup and lookup["course_id"] != self.course_id:
            raise BlackboardError("BB 评分表单课程 ID 不一致。")
        if not {"grade", "feedbacktext"}.issubset(lookup):
            raise BlackboardError("BB 评分表单缺少 grade/feedbacktext 字段，需要适配此版本。")
        if not any(re.search(r"nonce|csrf|token", key, re.I) and value for key, value in values):
            raise BlackboardError("BB 评分表单未包含新鲜的安全令牌；已阻止写入。")
        return form, values

    def _grade_target(self, response: requests.Response, form: Any, attempt: Attempt) -> str:
        """Resolve the observed static or inline-grader form, without running JS."""
        if str(form.get("method", "get")).lower() != "post":
            raise BlackboardError("BB 评分表单不是 POST 表单，已阻止写入。")

        def checked_url(raw: str, operation: str, *, require_course: bool = False) -> str:
            target = self._url(urljoin(response.url, raw))
            parsed = urlsplit(target)
            if not re.fullmatch(r"/webapps/assignment/+gradeAssignment/+" + operation, parsed.path):
                raise BlackboardError("BB 评分表单不是已识别的个人作业提交地址，已阻止写入。")
            query = parse_qs(parsed.query, keep_blank_values=True)
            expected = {"course_id": self.course_id, "attempt_id": attempt.id,
                        "attemptId": attempt.id, "currentAttemptId": attempt.id}
            if require_course and query.get("course_id") != [self.course_id]:
                raise BlackboardError("BB 内嵌评分地址缺少匹配的课程 ID，已阻止写入。")
            if any(key in query and query[key] != [value] for key, value in expected.items()):
                raise BlackboardError("BB 评分地址的课程或提交 ID 不一致，已阻止写入。")
            if parsed.fragment:
                raise BlackboardError("BB 评分地址包含无法识别的片段，已阻止写入。")
            return target

        action = str(form.get("action", "")).strip()
        if action:
            return checked_url(action, "submit")
        # USTC's current Original UI sets document.gradeAttemptForm.action from
        # the fourth inlineGrader argument in onSubmitClick; the HTML action and
        # submitGradeUrl hidden input are empty. Read only that literal shape.
        if (form.get("id") != "currentAttempt_form" or form.get("name") != "gradeAttemptForm"
                or re.sub(r"\s+", "", str(form.get("onsubmit", ""))) != "returnfalse;"
                or form.select_one('input[type="hidden"][name="submitGradeUrl"]') is None):
            raise BlackboardError("BB 空地址评分表单缺少已识别的内嵌评分结构，已阻止写入。")
        soup = BeautifulSoup(response.content, "html.parser")
        named_forms = soup.select('form[name="gradeAttemptForm"]')
        if len(named_forms) != 1 or named_forms[0].get("id") != "currentAttempt_form":
            raise BlackboardError("BB 内嵌评分表单名称不唯一，已阻止写入。")
        scripts = "\n".join(script.get_text() for script in soup.select("script:not([src])"))
        constructor = r"\battemptInlineGrader\s*=\s*new\s+attemptGrading\.inlineGrader\s*\("
        # Mask strings (including templates) and comments before locating code.
        # Keep offsets, then parse only literal arguments in the original text.
        non_code = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/\*[\s\S]*?\*/|//[^\r\n]*'''
        code_only = re.sub(non_code, lambda match: " " * len(match[0]), scripts)
        starts = list(re.finditer(constructor, code_only))
        literal = r"(?:'([^'\\\r\n]*)'|\"([^\"\\\r\n]*)\")"
        expression = constructor + r"\s*" + r"\s*,\s*".join([literal] * 5) + r"\s*\)\s*;"
        match = re.compile(expression).match(scripts, starts[0].start()) if len(starts) == 1 else None
        if match is None:
            raise BlackboardError("BB 内嵌评分提交地址缺失或不唯一，已阻止写入。")
        groups = match.groups()
        widget, draft, rubric_draft, submit, ajax = [
            groups[index] if groups[index] is not None else groups[index + 1]
            for index in range(0, 10, 2)
        ]
        if widget != "currentAttempt" or ajax != "false":
            raise BlackboardError("BB 内嵌评分模式尚不支持，已阻止写入。")
        checked_url(draft, "saveDraft", require_course=True)
        checked_url(rubric_draft, "saveRubricDraft", require_course=True)
        target = checked_url(submit, "submit", require_course=True)
        hidden = str(form.select_one('input[name="submitGradeUrl"]').get("value", "")).strip()
        if hidden and checked_url(hidden, "submit", require_course=True) != target:
            raise BlackboardError("BB 内嵌评分地址互相冲突，已阻止写入。")
        return target

    def _prepare_grade_upload(self, attempt: Attempt, score: float, feedback: str) -> tuple[str, str, str, float, dict[str, Any]]:
        """All checks and read-only requests finish before returning a write plan."""
        # Re-read the maximum score before every write, including after reconnects.
        current_assignments = self.list_assignments()
        assignment = next((item for item in current_assignments if item.id == attempt.assignment_id), None)
        if assignment is None:
            raise BlackboardError("待上传作业不在当前课程的作业列表中。")
        score = float(score)
        if not math.isfinite(score) or not 0 <= score <= assignment.max_score:
            raise ValueError(f"成绩必须在 0 到 {assignment.max_score:g} 之间。")
        if not isinstance(feedback, str):
            raise ValueError("审核后的评语必须为字符串，可以为空。")
        if not self._students:
            self.list_students()
        if attempt.student_id not in {student.student_id for student in self._students.values()}:
            raise BlackboardError("待上传学生不在当前课程的名单中。")
        # Re-read attempt membership immediately before writing (not cached data).
        live_attempts = (self._legacy_attempts(attempt.assignment_id, attempt.student_id)
                         if self._select_mode() == "legacy" else self.list_attempts(attempt.assignment_id))
        matching_attempts = [item for item in live_attempts
                             if item.student_id == attempt.student_id and item.assignment_id == attempt.assignment_id]
        if not any(item.id == attempt.id
                   and item.status != "NeedsReconciliation" for item in matching_attempts):
            raise BlackboardError("该学生的提交已变化或被删除，请重新同步并审核。")
        try:
            latest = latest_attempts([asdict(item) for item in matching_attempts])
        except ValueError as exc:
            raise BlackboardError("无法确定该学生的最新提交，请重新同步并核对提交时间后再上传。") from exc
        if not latest or latest[0]["id"] != attempt.id:
            raise BlackboardError("该学生已有更新的提交，只能上传最新提交的成绩；请重新同步并审核最新版本。")
        url = self._attempt_url(attempt)
        fresh = self._request("GET", url, headers={"Cache-Control": "no-cache"})
        if self._select_mode() == "rest":
            self._verify_rest_identity(self._json(fresh), attempt)
            headers = {"Referer": self._endpoint("grade_center")}
            csrf = next((cookie.value for cookie in self.session.cookies if cookie.name in {"XSRF-TOKEN", "Bb-XSRF"}), "")
            if csrf:
                headers["X-XSRF-TOKEN"] = unquote(csrf)
            if not csrf and not self.session.headers.get("Authorization"):
                raise BlackboardError("REST 写入缺少 OAuth token 或 CSRF token；请配置学校授权的 REST token。")
            if fresh.headers.get("ETag"):
                headers["If-Match"] = fresh.headers["ETag"]
            return "rest", url, url, score, {
                "json": {"score": score, "feedback": html.escape(feedback), "status": "Completed"},
                "headers": headers,
            }
        form, values = self._grade_form(fresh, attempt)
        target = self._grade_target(fresh, form, attempt)
        values = [(key, str(score) if key == "grade" else html.escape(feedback) if key == "feedbacktext" else value) for key, value in values]
        options: dict[str, Any] = {"headers": {"Referer": fresh.url}}
        if form.get("enctype", "").lower() == "multipart/form-data":
            options["files"] = [(key, (None, value)) for key, value in values]
        else:
            options["data"] = values
        return "legacy", url, target, score, options

    def upload_grade(self, attempt: Attempt, score: float, feedback: str) -> dict[str, Any]:
        """Write one reviewed grade exactly once, then re-fetch to verify it.

        Only preparation errors are definitely unsent. Every error after starting
        the write is uncertain, including malformed responses and readback errors.
        """
        try:
            mode, url, target, score, options = self._prepare_grade_upload(attempt, score, feedback)
        except (BlackboardError, ValueError, TypeError, OverflowError) as exc:
            raise GradeUploadBlockedError(str(exc)) from exc
        if mode == "rest":
            try:
                self._request("PATCH", target, **options)
            except Exception as exc:
                raise GradeVerificationError("成绩写入结果不确定。请先在 BB 核对该提交，勿直接重试。") from exc
            try:
                returned = self._json(self._request("GET", url, headers={"Cache-Control": "no-cache"}))
                self._verify_rest_identity(returned, attempt)
                if "feedback" not in returned:
                    raise BlackboardError("BB 回读缺少评语字段，无法核对最终评语。")
                verified_score, verified_feedback = returned.get("score"), _text(returned.get("feedback"))
            except Exception as exc:
                raise GradeVerificationError("成绩已发送，但 BB 回读失败。请在 BB 核对后再处理。") from exc
        else:
            try:
                response = self._request("POST", target, **options)
                if BeautifulSoup(response.content, "html.parser").select_one("#badMsg1, .bad, .error"):
                    raise BlackboardError("BB 返回了评分错误提示。")
            except Exception as exc:
                raise GradeVerificationError("成绩写入结果不确定。请在 BB 核对当前提交，勿直接重试。") from exc
            try:
                _, readback = self._grade_form(self._request("GET", url, headers={"Cache-Control": "no-cache"}), attempt)
                returned = dict(readback)
                verified_score, verified_feedback = returned.get("grade"), _text(returned.get("feedbacktext"))
            except Exception as exc:
                raise GradeVerificationError("成绩已发送，但无法确认 BB 保存结果。请在 BB 人工核对。") from exc
        try:
            matches = math.isclose(float(verified_score), score, abs_tol=1e-6, rel_tol=0)
        except (ValueError, TypeError):
            matches = False
        if not matches or " ".join(verified_feedback.split()) != " ".join(feedback.split()):
            raise GradeVerificationError("BB 回读的分数或评语与审核结果不一致，请人工核对，勿直接重试。")
        self._overview_cache = None
        return {"verified": True, "attempt_id": attempt.id, "student_id": attempt.student_id,
                "assignment_id": attempt.assignment_id, "score": score, "feedback": feedback}
