"""Synthetic USTC inline grading forms; all HTTP traffic is mocked."""

import html

import pytest
import requests

from bb_assistant.blackboard import GradeUploadBlockedError, GradeVerificationError
from test_blackboard import ATTEMPT, BASE, COURSE, FakeSession, grade_page, response, upload_client


DRAFT = f"/webapps/assignment//gradeAssignment/saveDraft?course_id={COURSE}"
RUBRIC_DRAFT = f"/webapps/assignment//gradeAssignment/saveRubricDraft?course_id={COURSE}"
SUBMIT = f"/webapps/assignment//gradeAssignment/submit?course_id={COURSE}"


def inline_constructor(*, region="currentAttempt", draft=DRAFT, rubric=RUBRIC_DRAFT,
                       submit=SUBMIT, ajax="false"):
    return (
        "attemptInlineGrader = new attemptGrading.inlineGrader(\n"
        f"  '{region}', '{draft}', '{rubric}', '{submit}', '{ajax}'\n"
        ");"
    )


def inline_page(score="", feedback="", *, script=None, action="", attempt_id=ATTEMPT.id):
    """Keep the empty action/URL fields and JS-disabled submit from the actual form."""
    markup = grade_page(score, feedback, attempt_id=attempt_id).replace(
        'id="currentAttempt_form" method="post"',
        'id="currentAttempt_form" name="gradeAttemptForm" method="post" onsubmit="return false;"',
    ).replace('action="/webapps/assignment/gradeAssignment/submit"',
              f'action="{html.escape(action, quote=True)}"')
    markup = markup.replace("</form>", '<input type="hidden" name="submitGradeUrl" value="">'
                            '<input type="hidden" name="cancelGradeUrl" value=""></form>')
    return markup + "<script>" + (inline_constructor() if script is None else script) + "</script>"


@pytest.mark.parametrize("feedback", ["步骤清楚。", ""])
def test_inline_form_posts_once_to_literal_submit_and_verifies_readback(monkeypatch, feedback):
    session = FakeSession(response(inline_page("9", "旧评语")), response("saved"),
                          response(inline_page("10", feedback)))
    client = upload_client(session, monkeypatch)

    receipt = client.upload_grade(ATTEMPT, 10, feedback)

    assert receipt["verified"] is True
    assert receipt["score"] == 10
    assert receipt["feedback"] == feedback
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]
    method, target, options = session.calls[1]
    assert target == BASE + SUBMIT
    assert method == "POST"
    fields = {key: value for key, (_, value) in options["files"]}
    assert fields["grade"] == "10.0"
    assert fields["feedbacktext"] == feedback
    assert fields["attempt_id"] == ATTEMPT.id
    assert fields["course_id"] == COURSE
    assert fields["blackboard.platform.security.NonceUtil.nonce"] == "fresh-token"
    assert fields["gradingNotestext"] == "existing instructor note"
    assert "clearAttempt" not in fields


@pytest.mark.parametrize("script", [
    inline_constructor(submit="https://evil.example" + SUBMIT),
    inline_constructor(draft="https://evil.example" + DRAFT),
    inline_constructor(rubric="https://evil.example" + RUBRIC_DRAFT),
    inline_constructor(submit="http://bb.example.edu" + SUBMIT),
    inline_constructor(submit=SUBMIT.replace(COURSE, "_999_1")),
    inline_constructor(draft=DRAFT.replace(COURSE, "_999_1")),
    inline_constructor(rubric=RUBRIC_DRAFT.replace(COURSE, "_999_1")),
    inline_constructor(submit=SUBMIT + "&course_id=_999_1"),
    inline_constructor(submit=SUBMIT + "&attempt_id=_999_1"),
    inline_constructor(submit=SUBMIT + "&attemptId=_999_1"),
    inline_constructor(submit=SUBMIT + "&currentAttemptId=_999_1"),
    inline_constructor(submit="/webapps/assignment//gradeAssignment/saveDraft?course_id=" + COURSE),
    inline_constructor(draft=SUBMIT),
    inline_constructor(rubric=SUBMIT),
    inline_constructor(region="anotherAttempt"),
    inline_constructor(ajax="true"),
    inline_constructor() + "\n" + inline_constructor(),
    inline_constructor() + "\n" + inline_constructor(region="anotherAttempt"),
    inline_constructor().replace("'currentAttempt'", "getCurrentAttempt()"),
    inline_constructor().replace(f"'{SUBMIT}'", "getSubmitUrl()"),
    inline_constructor().replace(f"'{SUBMIT}'", f"'{SUBMIT}' + '&extra=1'"),
    inline_constructor().replace("'false'", "false"),
    inline_constructor().replace("'false'", "'false', 'extra'"),
    "// no constructor available",
])
def test_untrusted_or_ambiguous_inline_target_is_blocked_before_post(monkeypatch, script):
    session = FakeSession(response(inline_page(script=script)))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("old,new", [
    ('name="gradeAttemptForm"', 'name="unrelatedForm"'),
    ('onsubmit="return false;"', ''),
    ('method="post"', 'method="get"'),
    ('name="submitGradeUrl"', 'name="unrelatedField"'),
    ('name="attempt_id" value="_100_1"', 'name="attempt_id" value="_999_1"'),
    (f'name="course_id" value="{COURSE}"', 'name="course_id" value="_999_1"'),
])
def test_inline_constructor_must_belong_to_recognized_form(monkeypatch, old, new):
    session = FakeSession(response(inline_page().replace(old, new)))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("action", [
    "/unrelated/gradeAssignment/submit",
    "/webapps/assignment/gradeAssignment/saveDraft",
    "/webapps/assignment/gradeAssignment/submit/extra",
    "/webapps/assignment/gradeGroupAssignment/submit",
    "https://evil.example" + SUBMIT,
    "http://bb.example.edu" + SUBMIT,
    SUBMIT.replace(COURSE, "_999_1"),
    SUBMIT + "&attempt_id=_999_1",
    SUBMIT + "&attemptId=_999_1",
    SUBMIT + "&currentAttemptId=_999_1",
])
def test_nonempty_invalid_action_cannot_fall_back_to_valid_inline_constructor(monkeypatch, action):
    session = FakeSession(response(inline_page(action=action)))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("at_readback", [False, True])
def test_inline_post_or_readback_timeout_remains_unknown_and_is_never_retried(monkeypatch, at_readback):
    responses = [response(inline_page())]
    if at_readback:
        responses.append(response("saved"))
    responses.append(requests.Timeout("private-token-must-not-leak"))
    session = FakeSession(*responses)
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeVerificationError) as caught:
        client.upload_grade(ATTEMPT, 10, "")

    assert not isinstance(caught.value, GradeUploadBlockedError)
    assert "private-token-must-not-leak" not in str(caught.value)
    assert [call[0] for call in session.calls] == (["GET", "POST", "GET"] if at_readback else ["GET", "POST"])
    assert sum(call[0] == "POST" for call in session.calls) == 1


def test_inline_readback_mismatch_remains_unknown(monkeypatch):
    session = FakeSession(response(inline_page()), response("saved"), response(inline_page("9.5", "")))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeVerificationError, match="不一致"):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("duplicate", [
    '<input name="attempt_id" value="_999_1">',
    '<input name="attempt_id" value="_100_1">',
    f'<input name="course_id" value="{COURSE}">',
    '<input name="course_id" value="_999_1">',
    '<input name="grade" value="9.5">',
    '<textarea name="feedbacktext">conflicting comment</textarea>',
    '<input name="blackboard.platform.security.NonceUtil.nonce" value="other-token">',
])
def test_duplicate_sensitive_form_fields_are_blocked_before_post(monkeypatch, duplicate):
    # The conflicting field comes first: converting all pairs to a dict would
    # hide it behind the original valid value, but POST still sends both pairs.
    page = inline_page().replace('<input name="attempt_id"', duplicate + '<input name="attempt_id"', 1)
    session = FakeSession(response(page))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError, match="重复"):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("alias", ["attemptId", "currentAttemptId"])
def test_conflicting_identity_alias_cannot_hide_behind_valid_attempt_id(monkeypatch, alias):
    page = inline_page().replace("</form>", f'<input name="{alias}" value="_999_1"></form>')
    session = FakeSession(response(page))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("extra_form", [
    '<form id="currentAttempt_form" name="otherForm"></form>',
    '<form id="other_form" name="gradeAttemptForm"></form>',
])
def test_duplicate_form_identity_is_blocked_before_post(monkeypatch, extra_form):
    session = FakeSession(response(inline_page() + extra_form))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


IGNORED_CONSTRUCTORS = [
    "/* " + inline_constructor() + " */",
    "// " + inline_constructor().replace("\n", " "),
    'const example = "' + inline_constructor().replace("\n", " ") + '";',
    "const example = `" + inline_constructor() + "`;",
]


@pytest.mark.parametrize("script", IGNORED_CONSTRUCTORS)
def test_comment_or_string_constructor_does_not_authorize_submit(monkeypatch, script):
    session = FakeSession(response(inline_page(script=script)))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeUploadBlockedError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize("ignored", IGNORED_CONSTRUCTORS)
def test_ignored_constructor_does_not_make_real_constructor_ambiguous(monkeypatch, ignored):
    script = ignored + "\n" + inline_constructor()
    session = FakeSession(response(inline_page(script=script)), response("saved"),
                          response(inline_page("10", "", script=script)))
    client = upload_client(session, monkeypatch)

    assert client.upload_grade(ATTEMPT, 10, "")["verified"] is True

    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]
    assert session.calls[1][1] == BASE + SUBMIT


def test_duplicate_readback_score_is_uncertain_after_one_post(monkeypatch):
    readback = inline_page("10", "").replace("</form>", '<input name="grade" value="9.5"></form>')
    session = FakeSession(response(inline_page()), response("saved"), response(readback))
    client = upload_client(session, monkeypatch)

    with pytest.raises(GradeVerificationError):
        client.upload_grade(ATTEMPT, 10, "")

    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]
