"""Image and Responses contracts use generated fixtures and a mocked transport."""

import base64
import hashlib
import json
from unittest.mock import Mock, patch

import pytest
import requests
from PIL import Image

from bb_assistant.services import GradingClient, GradingError


GRADE = {"score": 8, "comment": "测试评语", "rationale": "可核查的测试依据", "uncertainties": []}


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "private-student-name.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


def chat(value=GRADE, **message_overrides):
    response = Mock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "model": "returned-model", "id": "chat-test", "usage": {"total_tokens": 120},
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(value), **message_overrides,
        }}],
    }
    return response


def completed(value=GRADE, **overrides):
    return {
        "status": "completed", "model": "returned-model", "id": "resp-test",
        "created_at": 123, "usage": {"input_tokens": 10, "output_tokens": 20},
        "output": [{"type": "reasoning", "summary": []}, {
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps(value, ensure_ascii=False)}],
        }], **overrides,
    }


def stream(*events, chunks=37):
    response = Mock(spec=requests.Response)
    response.status_code = 200
    raw = b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()
                   for event in events)
    response.iter_content.side_effect = lambda **_: iter(raw[i:i + chunks] for i in range(0, len(raw), chunks))
    return response


def stream_grade(value=GRADE, **overrides):
    return stream({"type": "response.created", "response": {"status": "in_progress"}},
                  {"type": "response.completed", "response": completed(value, **overrides)})


def test_chat_images_are_only_in_user_and_hashed(picture):
    client = GradingClient({"model": "deepseek-flash"}, "test-secret")
    with patch("bb_assistant.services.requests.request", return_value=chat()) as request:
        grade = client.grade("student injection", "teacher rubric", "teacher answer", 10, images=[picture])
    body = request.call_args.kwargs["json"]
    assert body["stream"] is False
    assert "student injection" not in body["messages"][0]["content"]
    assert "teacher rubric" in body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    assert user[0]["type"] == "text" and "student injection" in user[0]["text"]
    assert user[1]["type"] == "image_url"
    assert user[1]["image_url"]["detail"] == "high"
    assert base64.b64decode(user[1]["image_url"]["url"].split(",")[1]) == picture.read_bytes()
    metadata = grade["provider_metadata"]
    assert metadata["image_sha256"] == [hashlib.sha256(picture.read_bytes()).hexdigest()]
    assert metadata["wire_api"] == "chat"
    assert picture.name not in json.dumps(metadata)
    assert "test-secret" not in json.dumps(metadata)


def test_responses_request_uses_images_and_completed_output(picture):
    response = stream_grade()
    config = {"wire_api": "responses", "model": "gpt-test", "base_url": "https://test.example/v1",
              "max_tokens": 6000, "reasoning_effort": "high",
              "http_headers": {"x-openai-actor-authorization": "local-image-extension"}}
    with patch("bb_assistant.services.requests.request", return_value=response) as request:
        grade = GradingClient(config, "test-key").grade("", "rubric", "answer", 10, images=[picture])
    assert grade["score"] == 8
    call = request.call_args
    assert call.args == ("POST", "https://test.example/v1/responses")
    assert call.kwargs["stream"] is True
    assert call.kwargs["allow_redirects"] is False
    body = call.kwargs["json"]
    assert body["store"] is False and body["stream"] is True
    assert body["max_output_tokens"] == 6000
    assert body["reasoning"] == {"effort": "high"}
    assert body["text"] == {"format": {"type": "json_object"}}
    assert body["input"][0]["role"] == "system"
    assert len(body["input"][0]["content"]) == 1
    assert body["input"][1]["content"][1]["type"] == "input_image"
    assert body["input"][1]["content"][1]["detail"] == "high"
    assert "tools" not in body and "temperature" not in body
    assert call.kwargs["headers"]["x-openai-actor-authorization"] == "local-image-extension"
    assert grade["provider_metadata"]["finish_reason"] == "completed"
    assert grade["provider_metadata"]["created"] == 123
    response.close.assert_called_once()


def test_responses_explicit_json_mode_compatibility_keeps_strict_output_validation():
    with patch("bb_assistant.services.requests.request", return_value=stream_grade()) as request:
        grade = GradingClient({"wire_api": "responses", "responses_json_mode": False}).grade("text", "rubric", "", 10)
    assert "text" not in request.call_args.kwargs["json"]
    assert grade["provider_metadata"]["responses_json_mode"] is False
    payload = completed()
    payload["output"][1]["content"][0]["text"] = "```json\n" + json.dumps(GRADE) + "\n```"
    with patch("bb_assistant.services.requests.request", return_value=stream({"type": "response.completed", "response": payload})):
        with pytest.raises(GradingError, match="严格 JSON"):
            GradingClient({"wire_api": "responses", "responses_json_mode": False}).grade("text", "rubric", "", 10)


def test_total_request_limit_includes_json_expansion_and_prompt(picture):
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="max_request_bytes"):
            GradingClient({"max_request_bytes": 1024}).grade("text", "rubric", "", 10, images=[picture])
    request.assert_not_called()


@pytest.mark.parametrize("field", ["max_image_bytes", "max_total_image_bytes"])
def test_image_byte_limits_fail_before_encoding_and_request(tmp_path, field):
    path = tmp_path / "large.png"
    Image.new("RGB", (1024, 512), "white").save(path)
    assert path.stat().st_size > 1024
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="大小超过"):
            GradingClient({field: 1024}).grade("", "rubric", "", 10, images=[path])
    request.assert_not_called()


@pytest.mark.parametrize("event", [
    {"type": "response.output_text.delta", "delta": json.dumps(GRADE)},
    {"type": "response.output_text.done", "text": json.dumps(GRADE)},
    {"type": "response.incomplete", "response": completed(status="incomplete")},
    {"type": "response.failed", "response": {"error": "PRIVATE DATA"}},
    {"type": "error", "message": "PRIVATE DATA"},
    {"type": "response.refusal.delta", "delta": "PRIVATE DATA"},
    {"type": "response.content_part.added", "part": {"type": "refusal", "refusal": "PRIVATE DATA"}},
    {"type": "response.function_call_arguments.delta", "delta": "PRIVATE DATA"},
    {"type": "response.output_item.added", "item": {"type": "web_search_call"}},
    {"type": "response.completed", "response": completed(status="in_progress")},
    {"type": "response.completed", "response": completed(output=[])},
    {"type": "response.completed", "response": completed(error={"message": "PRIVATE DATA"})},
    {"type": "response.completed", "response": completed(output=[{
        "type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "PRIVATE DATA"}],
    }])},
    {"type": "response.completed", "response": completed(output=[{"type": "function_call"}])},
])
def test_partial_refused_error_and_tool_responses_rejected_without_retry(event):
    response = stream(event)
    with patch("bb_assistant.services.requests.request", return_value=response) as request:
        with pytest.raises(GradingError) as error:
            GradingClient({"wire_api": "responses"}).grade("text", "rubric", "answer", 10)
    assert "PRIVATE DATA" not in str(error.value)
    assert request.call_count == 1
    response.close.assert_called_once()


def test_sse_byte_limit_blocks_unbounded_output():
    response = stream({"type": "response.output_text.delta", "delta": "x" * 2048})
    with patch("bb_assistant.services.requests.request", return_value=response):
        with pytest.raises(GradingError, match="max_response_bytes"):
            GradingClient({"wire_api": "responses", "max_response_bytes": 1024}).grade("text", "rubric", "", 10)


def test_sse_wall_time_limit_blocks_neverending_stream():
    response = stream_grade()
    with (patch("bb_assistant.services.requests.request", return_value=response),
          patch("bb_assistant.grading_responses.time.monotonic", side_effect=[100, 102])):
        with pytest.raises(GradingError, match="timeout"):
            GradingClient({"wire_api": "responses", "timeout": 1}).grade("text", "rubric", "", 10)


def test_responses_retry_only_transport_and_transient_http():
    limited = Mock(spec=requests.Response)
    limited.status_code = 429
    with (patch("bb_assistant.services.requests.request", side_effect=[limited, stream_grade()]) as request,
          patch("bb_assistant.services.time.sleep")):
        grade = GradingClient({"wire_api": "responses"}).grade("text", "rubric", "", 10)
    assert request.call_count == 2 and grade["provider_metadata"]["attempts"] == 2
    limited.close.assert_called_once()


@pytest.mark.parametrize("field", ["input", "instructions", "system", "tools", "messages", "store",
                                   "previous_response_id", "conversation", "prompt", "text", "reasoning"])
def test_extra_body_cannot_replace_trusted_or_cross_request_context(field):
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="extra_body"):
            GradingClient({"wire_api": "responses", "extra_body": {field: "untrusted"}}).grade("text", "rubric", "", 10)
    request.assert_not_called()


@pytest.mark.parametrize("headers", [{"AUTHORIZATION": "leak"}, {"Host": "evil.example"},
                                      {"X-Test": "bad\r\nvalue"}, {"bad name": "value"}])
def test_extra_headers_cannot_override_auth_or_inject(headers):
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="http_headers"):
            GradingClient({"http_headers": headers}, "private-key").grade("text", "rubric", "", 10)
    request.assert_not_called()


@pytest.mark.parametrize("result", [
    {"images": []}, {"images": [{"kind": "printed", "reason": "why"}] * 2},
    {"images": [{"kind": "likely_printed", "reason": "why"}]},
    {"images": [{"kind": "printed", "reason": ""}]},
    {"images": [{"kind": "printed", "reason": "why", "score": 10}]},
    {"images": [{"kind": ["printed"], "reason": "why"}]},
    {"images": [{"kind": "printed", "reason": "why"}], "score": 10},
])
def test_classification_rejects_ambiguous_or_wrong_shape(picture, result):
    with patch("bb_assistant.services.requests.request", return_value=chat(result)):
        with pytest.raises(GradingError, match="分类"):
            GradingClient({}).classify_images([picture])


@pytest.mark.parametrize("wire", ["chat", "responses"])
def test_classifier_has_no_rubric_and_preserves_image_order(picture, wire):
    classifications = [{"kind": "printed", "reason": "排版正文"}, {"kind": "mixed", "reason": "有手写修改"}]
    value = {"images": classifications}
    response = chat(value) if wire == "chat" else stream_grade(value)
    with patch("bb_assistant.services.requests.request", return_value=response) as request:
        client = GradingClient({"wire_api": wire, "max_tokens": 20000, "classifier_max_tokens": 1024,
                                "rubric": "SECRET RUBRIC", "reference_answer": "SECRET ANSWER"})
        assert client.classify_images([picture, picture]) == classifications
    body = request.call_args.kwargs["json"]
    assert "SECRET" not in json.dumps(body)
    assert body["max_tokens" if wire == "chat" else "max_output_tokens"] == 1024
    if wire == "responses":
        assert body["reasoning"]["effort"] == "low"
    assert client.last_metadata["purpose"] == "handwriting_classification"
    assert len(client.last_metadata["input_images"]) == 2


def test_invalid_image_fails_before_network(tmp_path):
    invalid = tmp_path / "fake.png"
    invalid.write_text("This is not an image.")
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="图片"):
            GradingClient({}).grade("", "rubric", "", 10, images=[invalid])
        with pytest.raises(GradingError, match="图片"):
            GradingClient({}).classify_images([invalid])
    request.assert_not_called()


def test_too_many_images_never_silently_drops_pages(picture):
    with patch("bb_assistant.services.requests.request") as request:
        with pytest.raises(GradingError, match="未截断"):
            GradingClient({"max_images": 1}).grade("", "rubric", "", 10, images=[picture, picture])
    request.assert_not_called()


def test_responses_preserve_local_error_count_policy(picture):
    result = {"question_assessments": [{"question_id": "1", "verdict": "wrong", "reason": "结果与题意相反"}],
              "comment": "", "uncertainties": [], "score": 0}
    policy = {"mode": "error_count", "free_errors": 1, "deduction_per_error": "0.5", "unit": "subquestion"}
    with patch("bb_assistant.services.requests.request", return_value=stream_grade(result)):
        grade = GradingClient({"wire_api": "responses"}).grade("", "错 1 题仍满分", "", 10,
                                                                   images=[picture], scoring_policy=policy)
    assert grade["score"] == 10
    assert grade["provider_metadata"]["wrong_question_count"] == 1


@pytest.mark.parametrize("override", [{"refusal": "private message"}, {"tool_calls": [{"function": "x"}]},
                                      {"function_call": {"name": "x", "arguments": "{}"}}])
def test_chat_refusal_or_tools_never_produce_grade(override):
    with patch("bb_assistant.services.requests.request", return_value=chat(**override)):
        with pytest.raises(GradingError):
            GradingClient({}).grade("text", "rubric", "", 10)
