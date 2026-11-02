"""GeminiService dengan SDK Google asli di atas transport tiruan: bentuk request dan parsing respons."""

import json

import httpx
import pytest

from app.core.config import Settings
from app.services.audits import Extraction
from app.services.grading import RubricGrade
from app.services.llm import GeminiService, LLMCallInfo, LLMError, LLMService, get_llm
from app.services.prompts import EXTRACTION_SCHEMA, RUBRIC_SCHEMA

pytestmark = pytest.mark.anyio

USAGE = {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 8}


def _reply(text: str, finish: str = "STOP") -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": USAGE,
    }


def _sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode()


def _service(handler) -> tuple[GeminiService, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GeminiService(Settings(gemini_api_key="test-key"), http_client=client), seen


async def _extract(service: GeminiService, info: LLMCallInfo | None = None) -> Extraction:
    return await service.generate_json(
        tier="main", system="aturan", prompt="cerita", schema=EXTRACTION_SCHEMA, model_cls=Extraction, info=info
    )


async def test_generate_json_request_shape():
    payload = {"tasks": [{"description": "Merekap data", "hours_per_week": None, "uses_ai": True}]}
    service, seen = _service(lambda r: httpx.Response(200, json=_reply(json.dumps(payload))))
    info = LLMCallInfo()
    result = await _extract(service, info)
    assert result.tasks[0].description == "Merekap data" and result.tasks[0].hours_per_week is None
    assert (info.model, info.input_tokens, info.output_tokens) == ("gemini-3.8-flash", 120, 38)

    request = seen[0]
    assert "gemini-3.8-flash:generateContent" in str(request.url)
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "aturan"
    assert body["contents"] == [{"role": "user", "parts": [{"text": "cerita"}]}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == EXTRACTION_SCHEMA
    assert config["maxOutputTokens"] == 8000
    assert "temperature" not in config


async def test_rubric_schema_is_accepted_by_sdk():
    grade = {"criteria": [{"id": "r1", "level": 3, "feedback": "Baik."}], "overall_feedback": "Bagus."}
    service, seen = _service(lambda r: httpx.Response(200, json=_reply(json.dumps(grade))))
    result = await service.generate_json(
        tier="main", system="s", prompt="p", schema=RUBRIC_SCHEMA, model_cls=RubricGrade
    )
    assert result.criteria[0].level == 3
    assert json.loads(seen[0].content)["generationConfig"]["responseJsonSchema"] == RUBRIC_SCHEMA


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=_reply('{"tasks": "bukan list"}')),
        httpx.Response(200, json=_reply('{"tasks": [', finish="MAX_TOKENS")),
        httpx.Response(200, json=_reply("", finish="SAFETY")),
        httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
        httpx.Response(429, json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
        httpx.Response(500, json={"error": {"code": 500, "message": "x", "status": "INTERNAL"}}),
    ],
)
async def test_generate_json_failures_become_llm_error(response):
    service, _ = _service(lambda r: response)
    with pytest.raises(LLMError):
        await _extract(service)


async def test_connection_error_becomes_llm_error():
    def fail(request):
        raise httpx.ConnectError("putus", request=request)

    service, _ = _service(fail)
    with pytest.raises(LLMError):
        await _extract(service)


def _stream_response(*chunks: dict) -> httpx.Response:
    return httpx.Response(200, content=_sse(*chunks), headers={"content-type": "text/event-stream"})


async def test_stream_text_and_roles():
    last = {**_reply("dunia."), "usageMetadata": USAGE}
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Halo "}]}}]}
    service, seen = _service(lambda r: _stream_response(first, last))
    info = LLMCallInfo()
    messages = [
        {"role": "user", "content": "hai"},
        {"role": "assistant", "content": "ya"},
        {"role": "user", "content": "lanjut"},
    ]
    chunks = [c async for c in service.stream_text(tier="light", system="s", messages=messages, info=info)]
    assert "".join(chunks) == "Halo dunia."
    assert info.model == "gemini-3.5-flash-lite" and info.output_tokens == 38
    assert "gemini-3.5-flash-lite:streamGenerateContent" in str(seen[0].url)
    assert [c["role"] for c in json.loads(seen[0].content)["contents"]] == ["user", "model", "user"]


async def test_stream_blocked_raises_after_partial_text():
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Sebagian "}]}}]}
    service, _ = _service(lambda r: _stream_response(first, _reply("", finish="SAFETY")))
    received = []
    with pytest.raises(LLMError):
        async for chunk in service.stream_text(tier="main", system="s", messages=[{"role": "user", "content": "x"}]):
            received.append(chunk)
    assert received == ["Sebagian "]


async def test_stream_quota_error_becomes_llm_error():
    service, _ = _service(lambda r: httpx.Response(429, json={"error": {"code": 429, "message": "q"}}))
    with pytest.raises(LLMError):
        async for _ in service.stream_text(tier="main", system="s", messages=[{"role": "user", "content": "x"}]):
            pass


async def test_provider_selection(monkeypatch):
    import app.services.llm as llm_module

    get_llm.cache_clear()
    monkeypatch.setattr(llm_module, "get_settings", lambda: Settings(llm_provider="anthropic"))
    assert isinstance(get_llm(), LLMService)
    get_llm.cache_clear()
    monkeypatch.setattr(llm_module, "get_settings", lambda: Settings(llm_provider="gemini"))
    assert isinstance(get_llm(), GeminiService)
    get_llm.cache_clear()


def test_no_key_means_unavailable():
    assert GeminiService(Settings(gemini_api_key="")).available is False
