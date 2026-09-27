"""Synthetic protocol fixtures: no network, credentials, or student records."""

import json
from unittest.mock import AsyncMock

import pytest
import requests

from bb_assistant.blackboard import (
    Assignment, Attempt, BlackboardClient, BlackboardError, GradeUploadBlockedError, GradeVerificationError,
    Student, parse_dwr_attempts, safe_filename,
)


BASE = "https://bb.example.edu"
COURSE = "_12345_1"
ATTEMPT = Attempt("_100_1", "PB25000001", "20", BASE + "/unused")


def response(value="", status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = BASE + "/webapps/assignment/gradeAssignmentRedirector?attempt_id=_100_1"
    result.headers.update(headers or {})
    result.encoding = "utf-8"
    if isinstance(value, (dict, list)):
        result._content = json.dumps(value, ensure_ascii=False).encode()
        result.headers["Content-Type"] = "application/json"
    elif isinstance(value, bytes):
        result._content = value
    else:
        result._content = value.encode()
    # requests.Response.iter_content handles an already consumed body this way.
    result._content_consumed = True
    return result


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, f"Unexpected {method} request"
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def close(self):
        pass


def overview():
    return {"cachedBook": {
        "colDefs": [
            {"id": "20", "name": "作业一", "points": 100, "src": "resource/x-bb-assignment"},
            {"id": "total", "name": "Total", "src": "Calculated"},
        ],
        "rows": [
            [{"uid": "101", "avail": True}, {"c": "SI", "v": "PB25000001"},
             {"c": "FN", "v": "同学甲"}, {"c": "UN", "v": "pb25000001"}],
            [{"uid": "102", "avail": True}, {"c": "SI", "v": "PB25000263"},
             {"c": "FN", "v": "同学乙"}, {"c": "UN", "v": "pb25000263"}],
        ],
    }}


def grade_page(score="", feedback="", nonce="fresh-token", attempt_id="_100_1"):
    return f'''<div id="currentAttempt">
      <form id="currentAttempt_form" method="post" enctype="multipart/form-data"
        action="/webapps/assignment/gradeAssignment/submit">
        <input name="attempt_id" value="{attempt_id}">
        <input name="course_id" value="{COURSE}">
        <input name="blackboard.platform.security.NonceUtil.nonce" value="{nonce}">
        <input name="grade" id="currentAttempt_grade" value="{score}">
        <textarea name="feedbacktext">{feedback}</textarea>
        <textarea name="gradingNotestext">existing instructor note</textarea>
        <input name="disabled" disabled value="must-not-submit">
        <input type="checkbox" name="clearAttempt" value="1">
      </form></div>'''


def download_page(markup):
    return markup + '<form id="currentAttempt_form"><input name="attempt_id" value="_100_1"></form>'


def upload_client(session, monkeypatch, mode="legacy"):
    client = BlackboardClient(BASE, COURSE, mode=mode, session=session)
    client._students = {"101": Student("PB25000001", "同学甲", "101")}
    client._assignments = {"20": Assignment("20", "作业一", 100)}
    monkeypatch.setattr(client, "list_attempts", lambda assignment_id: [ATTEMPT])
    monkeypatch.setattr(client, "_legacy_attempts", lambda assignment_id, student_filter=None: [ATTEMPT])
    monkeypatch.setattr(client, "list_assignments", lambda: [Assignment("20", "作业一", 100)])
    return client


def test_legacy_roster_keeps_out_of_scope_students_visible():
    session = FakeSession(response(overview()), response(overview()))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    students = client.list_students()
    assert [(s.student_id, s.name) for s in students] == [
        ("PB25000001", "同学甲"), ("PB25000263", "同学乙"),
    ]
    assert client.list_assignments() == [Assignment("20", "作业一", 100)]
    assert len(session.calls) == 2


def test_duplicate_student_identity_stops_import():
    data = overview()
    data["cachedBook"]["rows"][1][1]["v"] = "PB25000001"
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=FakeSession(response(data)))
    with pytest.raises(BlackboardError, match="重复学号"):
        client.list_students()


def test_pagination_fetches_all_members_and_filters_non_student_roles():
    session = FakeSession(
        response({"results": [{"userId": "_101_1", "courseRoleId": "Student", "user": {
            "studentId": "PB23000001", "name": {"family": "测", "given": "试"},
        }}], "paging": {"nextPage": "/page-2"}}),
        response({"results": [
            {"userId": "_102_1", "courseRoleId": "Student", "user": {
                "userName": "PB24000002", "name": {"given": "学生二"}}},
            {"userId": "_staff_1", "courseRoleId": "Instructor"},
        ]}),
    )
    client = BlackboardClient(BASE, COURSE, mode="rest", session=session)
    assert [s.student_id for s in client.list_students()] == ["PB23000001", "PB24000002"]
    assert len(session.calls) == 2


def test_repeated_pagination_is_not_silent_partial_success():
    session = FakeSession(response({"results": [], "paging": {"nextPage": "/same"}}))
    client = BlackboardClient(BASE, COURSE, mode="rest", session=session)
    with pytest.raises(BlackboardError, match="分页重复"):
        client._paged(BASE + "/same")


def test_dwr_decode_preserves_empty_submissions_unicode_and_semicolons():
    source = '''throw 'allowScriptTagRemoting is false.';
      //#DWR-INSERT
      //#DWR-REPLY
      var s0={};s0.id="_100_1";s0.status=null;s0.date="2026/09/27";
      s0.comment="学生;作业";s0.score=0.0;s0.exempt=false;
      dwr.engine._remoteHandleCallback('0','0',[s0]);
      dwr.engine._remoteHandleCallback('0','1',[]);'''
    result = parse_dwr_attempts(source)
    assert result[0][0]["comment"] == "学生;作业"
    assert result[0][0]["status"] is None
    assert result[1] == []


@pytest.mark.parametrize("source", [
    "var s0=__import__('os').system('anything');dwr.engine._remoteHandleCallback('0','0',[s0]);",
    "var s0={};window.location='https://evil.example';dwr.engine._remoteHandleCallback('0','0',[s0]);",
    "dwr.engine._remoteHandleException('0','0',{message:'denied'});",
    "<html>login</html>",
])
def test_dwr_never_executes_unknown_code(source):
    with pytest.raises(BlackboardError):
        parse_dwr_attempts(source)


def test_legacy_attempt_discovery_maps_empty_and_submitted_students():
    session = FakeSession(
        response(overview()), response(overview()), response("<html>grade center</html>"),
        response('dwr.engine._origScriptSessionId = "original";'),
        response('var s0={};s0.id="_100_1";s0.status=null;s0.date="2026-09-27";'
                 "dwr.engine._remoteHandleCallback('0','0',[s0]);"
                 "dwr.engine._remoteHandleCallback('0','1',[]);"),
    )
    session.cookies.set("JSESSIONID", "test-session", domain="bb.example.edu", path="/webapps/gradebook")
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    attempts = client.list_attempts("20")
    assert [(a.id, a.student_id) for a in attempts] == [("_100_1", "PB25000001")]
    method, url, kwargs = session.calls[-1]
    assert method == "POST" and url.endswith("getAttemptsInfo.dwr")
    assert kwargs["data"]["c1-param1"] == "string:102"
    assert kwargs["data"]["c0-param0"] == "number:12345"


def test_download_rejects_cross_origin_link_before_network(tmp_path):
    page = '<div id="currentAttempt"><ul id="currentAttempt_submissionList"><a class="dwnldBtn" '
    page += 'href="https://evil.example/steal?fileName=a.pdf">Download</a></ul></div>'
    session = FakeSession(response(download_page(page)))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    with pytest.raises(BlackboardError, match="跨站"):
        client.download_attempt(ATTEMPT, tmp_path)
    assert len(session.calls) == 1


def test_download_sanitizes_windows_paths_and_keeps_duplicate_names(tmp_path):
    page = '''<div id="currentAttempt"><ul id="currentAttempt_submissionList">
        <a class="dwnldBtn" href="/download/1?fileName=..%2F..%2Fa.pdf">Download</a>
        <a class="dwnldBtn" href="/download/2?fileName=a.pdf">Download</a>
        </ul><div id="submissionTextView">这是正文</div></div>'''
    session = FakeSession(response(download_page(page)), response(b"%PDF-test-one"), response(b"%PDF-test-two"))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    paths = client.download_attempt(ATTEMPT, tmp_path)
    assert [p.name for p in paths] == ["a.pdf", "2_a.pdf", "submission_text.txt"]
    assert all(p.parent == tmp_path for p in paths)
    assert paths[1].read_bytes() == b"%PDF-test-two"
    assert paths[2].read_text(encoding="utf-8") == "这是正文"
    assert not list(tmp_path.glob("*.part"))


def test_download_login_response_does_not_become_pdf(tmp_path):
    page = '''<div id="currentAttempt"><ul id="currentAttempt_submissionList">
      <a class="dwnldBtn" href="/download?fileName=a.pdf">Download</a></ul></div>'''
    session = FakeSession(response(download_page(page)), response(b"<!DOCTYPE html><html>log in</html>"))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    with pytest.raises(BlackboardError, match="HTML"):
        client.download_attempt(ATTEMPT, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name,expected", [("../../a.pdf", "a.pdf"), (r"C:\data\CON.pdf", "_CON.pdf"),
                                            ("LPT1.txt", "_LPT1.txt"), (" a:b?.pdf ", "a_b_.pdf")])
def test_windows_attachment_names(name, expected):
    assert safe_filename(name) == expected


def test_upload_uses_fresh_nonce_preserves_notes_and_verifies_both_values(monkeypatch):
    session = FakeSession(response(grade_page()), response('<span id="goodMsg1">saved</span>'),
                          response(grade_page("86", "步骤清晰，结论正确。", "readback-token")))
    client = upload_client(session, monkeypatch)
    result = client.upload_grade(ATTEMPT, 86, "步骤清晰，结论正确。")
    assert result["verified"] is True
    method, _, kwargs = session.calls[1]
    assert method == "POST"
    fields = {key: value for key, (_, value) in kwargs["files"]}
    assert fields["blackboard.platform.security.NonceUtil.nonce"] == "fresh-token"
    assert fields["grade"] == "86.0"
    assert fields["gradingNotestext"] == "existing instructor note"
    assert "clearAttempt" not in fields and "disabled" not in fields
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("readback_feedback", ["", "<p><br></p>"])
def test_legacy_upload_clears_final_feedback_and_verifies_blank(monkeypatch, readback_feedback):
    session = FakeSession(response(grade_page("70", "旧评语")), response("saved"),
                          response(grade_page("80", readback_feedback)))
    client = upload_client(session, monkeypatch)
    receipt = client.upload_grade(ATTEMPT, 80, "")
    fields = {key: value for key, (_, value) in session.calls[1][2]["files"]}
    assert fields["grade"] == "80.0"
    assert fields["feedbacktext"] == ""
    assert receipt["verified"] is True
    assert receipt["feedback"] == ""
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


def test_legacy_blank_feedback_rejects_nonblank_readback(monkeypatch):
    session = FakeSession(response(grade_page("70", "旧评语")), response("saved"),
                          response(grade_page("80", "旧评语")))
    client = upload_client(session, monkeypatch)
    with pytest.raises(GradeVerificationError, match="不一致"):
        client.upload_grade(ATTEMPT, 80, "")
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("kwargs", [{"nonce": ""}, {"attempt_id": "_999_1"}])
def test_invalid_fresh_form_cannot_post(monkeypatch, kwargs):
    session = FakeSession(response(grade_page(**kwargs)))
    client = upload_client(session, monkeypatch)
    with pytest.raises(BlackboardError):
        client.upload_grade(ATTEMPT, 80, "正确")
    assert len(session.calls) == 1


def test_no_write_if_attempt_ownership_changed(monkeypatch):
    session = FakeSession()
    client = upload_client(session, monkeypatch)
    monkeypatch.setattr(client, "_legacy_attempts", lambda *_: [Attempt("_100_1", "PB25000263", "20", "")])
    with pytest.raises(BlackboardError, match="重新同步"):
        client.upload_grade(ATTEMPT, 80, "正确")
    assert session.calls == []


@pytest.mark.parametrize("mode", ["legacy", "rest"])
@pytest.mark.parametrize("old_date,new_id,new_date", [
    ("26-9-26", "_99_1", "26-9-27"),
    ("26-9-27", "_101_1", "26-9-27"),
    ("", "_101_1", ""),
])
def test_upload_rejects_older_live_attempt_before_grade_request(monkeypatch, mode, old_date, new_id, new_date):
    session = FakeSession()
    client = upload_client(session, monkeypatch, mode=mode)
    live = [
        Attempt(ATTEMPT.id, ATTEMPT.student_id, ATTEMPT.assignment_id, "", old_date),
        Attempt(new_id, ATTEMPT.student_id, ATTEMPT.assignment_id, "", new_date),
    ]
    monkeypatch.setattr(client, "list_attempts", lambda _: live)
    monkeypatch.setattr(client, "_legacy_attempts", lambda *_: live)
    with pytest.raises(BlackboardError, match="最新提交"):
        client.upload_grade(ATTEMPT, 80, "")
    assert session.calls == []


@pytest.mark.parametrize("mode", ["legacy", "rest"])
@pytest.mark.parametrize("old_status", ["reviewed", "Completed"])
@pytest.mark.parametrize("old_id,old_date", [("_101_1", "26-9-26"), ("_99_1", "26-9-27")])
def test_latest_upload_is_allowed_despite_older_reviewed_attempts(monkeypatch, mode, old_status, old_id, old_date):
    if mode == "legacy":
        session = FakeSession(response(grade_page()), response("saved"), response(grade_page("80", "")))
    else:
        session = FakeSession(
            response({"id": ATTEMPT.id, "userId": "101", "score": 0}),
            response({"id": ATTEMPT.id, "score": 80}),
            response({"id": ATTEMPT.id, "userId": "101", "score": 80, "feedback": ""}),
        )
    client = upload_client(session, monkeypatch, mode=mode)
    client.session.headers["Authorization"] = "Bearer test-secret"
    live = [
        Attempt(old_id, ATTEMPT.student_id, ATTEMPT.assignment_id, "", old_date, old_status),
        Attempt(ATTEMPT.id, ATTEMPT.student_id, ATTEMPT.assignment_id, "", "26-9-27"),
        Attempt("_999_1", "PB25000002", ATTEMPT.assignment_id, "", "26-9-28"),
        Attempt("_999_2", ATTEMPT.student_id, "another-assignment", "", "26-9-28"),
    ]
    monkeypatch.setattr(client, "list_attempts", lambda _: live)
    monkeypatch.setattr(client, "_legacy_attempts", lambda *_: live)
    assert client.upload_grade(ATTEMPT, 80, "")["verified"] is True
    assert [call[0] for call in session.calls] == ["GET", "POST" if mode == "legacy" else "PATCH", "GET"]


def test_latest_attempt_selection_failure_blocks_upload(monkeypatch):
    session = FakeSession()
    client = upload_client(session, monkeypatch)

    def cannot_select(_rows):
        raise ValueError("unrecognized timestamp and opaque attempt ID")

    monkeypatch.setattr("bb_assistant.blackboard.latest_attempts", cannot_select)
    with pytest.raises(BlackboardError, match="无法确定.*最新提交"):
        client.upload_grade(ATTEMPT, 80, "")
    assert session.calls == []


def test_readback_mismatch_is_not_reported_as_success(monkeypatch):
    session = FakeSession(response(grade_page()), response("saved"), response(grade_page("79", "旧评语")))
    client = upload_client(session, monkeypatch)
    with pytest.raises(GradeVerificationError, match="不一致"):
        client.upload_grade(ATTEMPT, 80, "新评语")
    assert len([call for call in session.calls if call[0] == "POST"]) == 1


def test_timeout_after_grade_write_is_uncertain_and_never_retried(monkeypatch):
    session = FakeSession(response(grade_page()), requests.Timeout("URL-with-private-token"))
    client = upload_client(session, monkeypatch)
    with pytest.raises(GradeVerificationError, match="勿直接重试") as exc:
        client.upload_grade(ATTEMPT, 80, "正确")
    assert "private-token" not in str(exc.value)
    assert len(session.calls) == 2


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1, 101])
def test_invalid_grade_is_rejected_before_any_network(monkeypatch, score):
    session = FakeSession()
    client = upload_client(session, monkeypatch)
    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, score, "评语")
    assert session.calls == []


def test_redirect_cannot_leak_bearer_to_other_host():
    session = FakeSession(response("", 302, {"Location": "https://evil.example/"}))
    client = BlackboardClient(BASE, COURSE, session=session, access_token="test-secret")
    with pytest.raises(BlackboardError, match="跨站"):
        client._request("GET", BASE + "/download")
    assert len(session.calls) == 1


def test_pyustc_async_context_ticket_is_redeemed_only_at_bb(monkeypatch):
    from pyustc import CASClient

    cas = AsyncMock()
    cas.__aenter__.return_value = cas
    cas.get_ticket.return_value = "ST-test"
    monkeypatch.setattr(CASClient, "login_by_pwd", lambda username, password: cas)
    service = BASE + "/webapps/bb-SSOIntegrationDemo-BBLEARN/execute/authValidate/customLogin?authProviderId=_103_1"
    session = FakeSession(response("logged in"), response("grade center"))
    client = BlackboardClient(BASE, COURSE, cas_service=service, session=session)
    client.login("test-user", "test-password")
    cas.get_ticket.assert_awaited_once_with(service)
    assert session.calls[0][1] == service + "&ticket=ST-test"
    assert all("test-password" not in str(call) for call in session.calls)


def test_cas_service_discovered_from_actual_link_shape():
    login = '''<a href="https://passport.ustc.edu.cn/login?service=https%3A%2F%2Fbb.example.edu%2Fauth%3FauthProviderId%3D_103_1">统一身份认证</a>'''
    client = BlackboardClient(BASE, COURSE, session=FakeSession(response(login)))
    assert client.discover_cas_service() == BASE + "/auth?authProviderId=_103_1"


def test_cas_discovery_handles_nginx_gateway_preserving_http_service():
    session = FakeSession(
        response("", 302, {"Location": "/nginx_auth/?next=68656c6c6f"}),
        response('<a href="/nginx_auth/login.php?next=68656c6c6f">统一身份认证</a>'),
        response("", 302, {"Location": "https://passport.ustc.edu.cn/login?service="
                 "http%3A%2F%2Fbb.example.edu%2Fnginx_auth%2Flogin.php%3Fnext%3D68656c6c6f"}),
    )
    client = BlackboardClient(BASE, COURSE, session=session)
    assert client.discover_cas_service() == "http://bb.example.edu/nginx_auth/login.php?next=68656c6c6f"
    assert len(session.calls) == 3
    assert all(url.startswith(BASE) for _, url, _ in session.calls)


def test_http_service_identity_ticket_always_redeemed_over_https(monkeypatch):
    from pyustc import CASClient

    cas = AsyncMock()
    cas.__aenter__.return_value = cas
    cas.get_ticket.return_value = "ST-test"
    monkeypatch.setattr(CASClient, "login_by_pwd", lambda username, password: cas)
    service = "http://bb.example.edu/nginx_auth/login.php?next=123"
    session = FakeSession(response("logged in"), response("grade center"))
    client = BlackboardClient(BASE, COURSE, cas_service=service, session=session)
    client.login("test-user", "test-password")
    cas.get_ticket.assert_awaited_once_with(service)
    assert session.calls[0][1].startswith(BASE + "/nginx_auth/")


def test_refresh_observes_roster_and_maximum_score_changes():
    original, changed = overview(), overview()
    changed["cachedBook"]["rows"] = changed["cachedBook"]["rows"][:1]
    changed["cachedBook"]["colDefs"][0]["points"] = 50
    session = FakeSession(response(original), response(changed), response(changed))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    assert len(client.list_students()) == 2
    assert len(client.list_students()) == 1
    assert client.list_assignments()[0].max_score == 50


def test_rest_assignments_exclude_tests_and_unknown_provider():
    columns = [{"id": str(i), "name": kind, "grading": {"type": "Attempts"},
                "scoreProviderHandle": kind, "score": {"possible": 100}}
               for i, kind in enumerate(["resource/x-bb-assignment", "resource/x-bb-assessment", "other"])]
    client = BlackboardClient(BASE, COURSE, mode="rest",
                              session=FakeSession(response({"results": columns})))
    assert [assignment.id for assignment in client.list_assignments()] == ["0"]


def test_download_rejects_another_attempt_even_when_page_has_files(tmp_path):
    page = download_page('<div id="currentAttempt"><div id="submissionTextView">内容</div></div>')
    page = page.replace('value="_100_1"', 'value="_999_1"')
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=FakeSession(response(page)))
    with pytest.raises(BlackboardError, match="提交 ID"):
        client.download_attempt(ATTEMPT, tmp_path)
    assert not list(tmp_path.iterdir())


def test_download_blocks_partial_attachment_recognition(tmp_path):
    page = download_page('''<div id="currentAttempt"><ul id="currentAttempt_submissionList">
      <li>a.pdf <a class="dwnldBtn" href="/download/1">Download</a></li>
      <li>b.pdf <a class="unknown-download" href="/download/2">Download</a></li>
      </ul></div>''')
    session = FakeSession(response(page))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    with pytest.raises(BlackboardError, match="遗漏"):
        client.download_attempt(ATTEMPT, tmp_path)
    assert len(session.calls) == 1


def test_embedded_answers_are_not_silently_lost(tmp_path):
    page = download_page('''<div id="currentAttempt"><div id="submissionTextView">
      请看截图<img src="/answers.png"></div></div>''')
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=FakeSession(response(page)))
    with pytest.raises(BlackboardError, match="图片或嵌入内容"):
        client.download_attempt(ATTEMPT, tmp_path)


def test_colliding_attachment_names_never_overwrite_each_other(tmp_path):
    names = ["2_a.pdf", "a.pdf", "a.pdf", "submission_text.txt", "2_submission_text.txt"]
    page = '<div id="currentAttempt"><ul id="currentAttempt_submissionList">'
    page += "".join(f'<li>{name}<a class="dwnldBtn" href="/download/{i}">Download</a></li>'
                    for i, name in enumerate(names))
    page += '</ul><div id="submissionTextView">正文</div></div>'
    session = FakeSession(response(download_page(page)), *(response(f"file-{i}".encode()) for i in range(5)))
    client = BlackboardClient(BASE, COURSE, mode="legacy", session=session)
    paths = client.download_attempt(ATTEMPT, tmp_path)
    assert len({path.name for path in paths}) == 6
    assert [path.read_text(encoding="utf-8") for path in paths] == ["file-0", "file-1", "file-2", "file-3", "file-4", "正文"]


def test_plain_text_cpp_feedback_readback_does_not_strip_template(monkeypatch):
    import html

    feedback = "请使用 vector<int> 保存数据。"
    session = FakeSession(response(grade_page()), response("saved"),
                          response(grade_page("80", html.escape(html.escape(feedback)))))
    client = upload_client(session, monkeypatch)
    assert client.upload_grade(ATTEMPT, 80, feedback)["verified"] is True


def test_rest_upload_checks_identity_etag_and_readback(monkeypatch):
    session = FakeSession(
        response({"id": "_100_1", "userId": "101", "score": 0}, headers={"ETag": '"v1"'}),
        response({"id": "_100_1", "score": 80}),
        response({"id": "_100_1", "userId": "101", "score": 80, "feedback": "正确"}),
    )
    client = BlackboardClient(BASE, COURSE, mode="rest", access_token="test-secret", session=session)
    client._students = {"101": Student("PB25000001", "同学甲", "101")}
    monkeypatch.setattr(client, "list_assignments", lambda: [Assignment("20", "作业一", 100)])
    monkeypatch.setattr(client, "list_attempts", lambda _: [ATTEMPT])
    assert client.upload_grade(ATTEMPT, 80, "正确")["verified"] is True
    assert [call[0] for call in session.calls] == ["GET", "PATCH", "GET"]
    assert session.calls[1][2]["headers"]["If-Match"] == '"v1"'
    assert session.calls[1][2]["json"] == {"score": 80, "feedback": "正确", "status": "Completed"}


@pytest.mark.parametrize("readback_feedback", ["", None, "<p><br></p>"])
def test_rest_upload_sends_and_verifies_empty_final_feedback(monkeypatch, readback_feedback):
    session = FakeSession(
        response({"id": "_100_1", "userId": "101", "score": 70, "feedback": "旧评语"}),
        response({"id": "_100_1", "score": 80}),
        response({"id": "_100_1", "userId": "101", "score": 80, "feedback": readback_feedback}),
    )
    client = upload_client(session, monkeypatch, mode="rest")
    client.session.headers["Authorization"] = "Bearer test-secret"
    receipt = client.upload_grade(ATTEMPT, 80, "")
    assert session.calls[1][2]["json"] == {"score": 80, "feedback": "", "status": "Completed"}
    assert receipt["verified"] is True
    assert receipt["feedback"] == ""
    assert [call[0] for call in session.calls] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize("feedback_fields", [{}, {"feedback": "旧评语"}])
def test_rest_blank_feedback_requires_confirmed_blank_readback(monkeypatch, feedback_fields):
    session = FakeSession(
        response({"id": "_100_1", "userId": "101", "score": 70, "feedback": "旧评语"}),
        response({"id": "_100_1", "score": 80}),
        response({"id": "_100_1", "userId": "101", "score": 80, **feedback_fields}),
    )
    client = upload_client(session, monkeypatch, mode="rest")
    client.session.headers["Authorization"] = "Bearer test-secret"
    with pytest.raises(GradeVerificationError):
        client.upload_grade(ATTEMPT, 80, "")
    assert [call[0] for call in session.calls] == ["GET", "PATCH", "GET"]


def test_rest_write_rejects_wrong_user_before_patch(monkeypatch):
    session = FakeSession(response({"id": "_100_1", "userId": "another-user"}))
    client = BlackboardClient(BASE, COURSE, mode="rest", access_token="test-secret", session=session)
    client._students = {"101": Student("PB25000001", "同学甲", "101")}
    monkeypatch.setattr(client, "list_assignments", lambda: [Assignment("20", "作业一", 100)])
    monkeypatch.setattr(client, "list_attempts", lambda _: [ATTEMPT])
    with pytest.raises(BlackboardError, match="学号不一致"):
        client.upload_grade(ATTEMPT, 80, "正确")
    assert [call[0] for call in session.calls] == ["GET"]
