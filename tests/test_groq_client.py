"""GroqService dengan SDK Groq asli di atas transport tiruan: bentuk request dan parsing respons."""

import json

import httpx
import pytest

from app.core.config import Settings
from app.services.audits import Extraction
from app.services.llm import GroqService, LLMCallInfo, LLMError
from app.services.prompts import EXTRACTION_SCHEMA

pytestmark = pytest.mark.anyio

PAYLOAD = {"tasks": [{"description": "Merekap data", "hours_per_week": 5, "uses_ai": False}]}


def _reply(text: str, finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}, "logprobs": None}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def _chunk(text: str | None = None, finish: str | None = None, usage: dict | None = None) -> dict:
    delta = {"content": text} if text is not None else {}
    data = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        data["x_groq"] = {"usage": usage}
    return data


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _service(handler, **overrides) -> tuple[GroqService, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GroqService(Settings(groq_api_key="test-key", **overrides), http_client=client), seen


async def _extract(service: GroqService, info: LLMCallInfo | None = None) -> Extraction:
    return await service.generate_json(
        tier="main", system="aturan", prompt="cerita", schema=EXTRACTION_SCHEMA, model_cls=Extraction, info=info
    )


async def test_generate_json_request_shape():
    service, seen = _service(lambda r: httpx.Response(200, json=_reply(json.dumps(PAYLOAD))))
    info = LLMCallInfo()
    result = await _extract(service, info)
    assert result.tasks[0].description == "Merekap data"
    assert (info.model, info.input_tokens, info.output_tokens) == ("openai/gpt-oss-120b", 120, 30)

    body = json.loads(seen[0].content)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["messages"][0] == {"role": "system", "content": "aturan"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == EXTRACTION_SCHEMA
    assert body["include_reasoning"] is False and "reasoning_format" not in body


async def test_qwen_hides_reasoning():
    service, seen = _service(
        lambda r: httpx.Response(200, json=_reply(json.dumps(PAYLOAD))), groq_model_main="qwen/qwen3.8-27b"
    )
    await _extract(service)
    body = json.loads(seen[0].content)
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["reasoning_format"] == "hidden" and "include_reasoning" not in body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=_reply('{"tasks": "bukan list"}')),
        httpx.Response(200, json=_reply('{"tasks": [', finish="length")),
        httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"message": "limit"}}),
        httpx.Response(400, json={"error": {"message": "model tidak mendukung json_schema"}}),
        httpx.Response(500, json={"error": {"message": "x"}}),
    ],
)
async def test_failures_become_llm_error_without_retry(response):
    service, seen = _service(lambda r: response)
    with pytest.raises(LLMError):
        await _extract(service)
    assert len(seen) == 1  # kuota habis langsung turun ke cadangan


async def test_connection_error_becomes_llm_error():
    def fail(request):
        raise httpx.ConnectError("putus", request=request)

    service, _ = _service(fail)
    with pytest.raises(LLMError):
        await _extract(service)


async def test_stream_text_and_usage():
    usage = {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}
    service, seen = _service(lambda r: _sse(_chunk("Halo "), _chunk("dunia."), _chunk(finish="stop", usage=usage)))
    info = LLMCallInfo()
    messages = [{"role": "user", "content": "hai"}, {"role": "assistant", "content": "ya"}]
    chunks = [c async for c in service.stream_text(tier="light", system="s", messages=messages, info=info)]
    assert "".join(chunks) == "Halo dunia."
    assert (info.model, info.input_tokens, info.output_tokens) == ("llama-3.1-8b-instant", 50, 7)
    body = json.loads(seen[0].content)
    assert body["stream"] is True and [m["role"] for m in body["messages"]] == ["system", "user", "assistant"]
    assert "include_reasoning" not in body and "reasoning_format" not in body


async def test_stream_content_filter_raises():
    service, _ = _service(lambda r: _sse(_chunk("Sebagian "), _chunk(finish="content_filter")))
    received = []
    with pytest.raises(LLMError):
        async for chunk in service.stream_text(tier="main", system="s", messages=[{"role": "user", "content": "x"}]):
            received.append(chunk)
    assert received == ["Sebagian "]


def test_no_key_means_unavailable():
    assert GroqService(Settings(groq_api_key="")).available is False
