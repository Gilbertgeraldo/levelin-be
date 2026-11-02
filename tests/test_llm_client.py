"""LLMService dengan SDK Anthropic asli di atas transport tiruan: bentuk request dan parsing respons."""

import json

import anthropic
import httpx2
import pytest

from app.core.config import Settings
from app.services.audits import Extraction
from app.services.llm import SERVER_FALLBACK_BETA, LLMCallInfo, LLMError, LLMService
from app.services.prompts import EXTRACTION_SCHEMA

pytestmark = pytest.mark.anyio


def _message(text: str, stop_reason: str = "end_turn", model: str = "claude-opus-5-5") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


def _sse(chunks: list[str], model: str) -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {**_message("", model=model), "content": [], "stop_reason": None}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ]
    events += [
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": c}})
        for c in chunks
    ]
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 12}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _service(handler, **overrides) -> tuple[LLMService, list]:
    seen: list = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    service = LLMService(Settings(anthropic_api_key="test-key", **overrides))
    service._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record)),
    )
    return service, seen


async def test_generate_json_main_tier_request_shape():
    payload = {"tasks": [{"nama": "Merekap data", "frekuensi": "mingguan", "jam_per_minggu": 5, "alat": [], "ilo_id": "4311-1", "keyakinan": "tinggi", "alasan": "cocok"}]}
    service, seen = _service(lambda r: httpx2.Response(200, json=_message(json.dumps(payload))))
    info = LLMCallInfo()
    result = await service.generate_json(
        tier="main", system="sys", prompt="cerita", schema=EXTRACTION_SCHEMA, model_cls=Extraction,
        effort="low", info=info,
    )
    assert result.tasks[0].nama == "Merekap data" and result.tasks[0].ilo_id == "4311-1"
    assert (info.model, info.input_tokens, info.output_tokens) == ("claude-opus-5-5", 120, 30)

    request = seen[0]
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert SERVER_FALLBACK_BETA in request.headers["anthropic-beta"]
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}}
    assert "temperature" not in body and "thinking" not in body


async def test_generate_json_rejects_bad_output_and_refusal():
    service, _ = _service(lambda r: httpx2.Response(200, json=_message('{"tasks": "bukan list"}')))
    with pytest.raises(LLMError):
        await service.generate_json(tier="main", system="s", prompt="p", schema={}, model_cls=Extraction)

    service, _ = _service(lambda r: httpx2.Response(200, json=_message("", stop_reason="refusal")))
    with pytest.raises(LLMError):
        await service.generate_json(tier="main", system="s", prompt="p", schema={}, model_cls=Extraction)

    service, _ = _service(lambda r: httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "x"}}))
    with pytest.raises(LLMError):
        await service.generate_json(tier="main", system="s", prompt="p", schema={}, model_cls=Extraction)


async def test_stream_light_tier_has_no_fallback_param():
    service, seen = _service(
        lambda r: httpx2.Response(200, content=_sse(["Halo ", "dunia."], "claude-haiku-5-5"),
                                  headers={"content-type": "text/event-stream"})
    )
    info = LLMCallInfo()
    chunks = [c async for c in service.stream_text(
        tier="light", system="s", messages=[{"role": "user", "content": "hai"}], info=info
    )]
    assert "".join(chunks) == "Halo dunia."
    assert info.model == "claude-haiku-5-5"
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-haiku-5-5" and body["stream"] is True
    assert "fallbacks" not in body
    assert SERVER_FALLBACK_BETA not in seen[0].headers.get("anthropic-beta", "")
