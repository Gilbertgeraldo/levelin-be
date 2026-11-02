"""Klien LLM (Gemini, Groq, atau Claude, dipilih lewat LLM_PROVIDER).

Semua keluaran JSON divalidasi skema; pemanggil wajib punya fallback.
Tier "main" dipakai untuk ekstraksi tugas, atasan AI, dan penilaian rubrik.
Tier "light" dipakai untuk penjelasan peta dan narasi rencana (LLM ringan).
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import anthropic
import groq
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings

logger = logging.getLogger("levelin.llm")

Tier = Literal["main", "light"]
Effort = Literal["low", "medium", "high"]

# Model yang mendukung server-side fallback saat classifier menolak permintaan
SERVER_FALLBACK_MODELS = {
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-sonnet-5-5",
}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """LLM tidak tersedia, gagal, menolak, atau keluarannya tidak lolos skema."""


@dataclass
class LLMCallInfo:
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _fill_info(info: LLMCallInfo | None, message) -> None:
    if info is None or message is None:
        return
    info.model = getattr(message, "model", None)
    usage = getattr(message, "usage", None)
    if usage is not None:
        info.input_tokens = getattr(usage, "input_tokens", 0) or 0
        info.output_tokens = getattr(usage, "output_tokens", 0) or 0


class _CachedPing:
    """Hasil cek penyedia LLM disimpan sebentar supaya /health tetap murah."""

    PING_TTL_SECONDS = 60.0
    _ping_result: bool | None = None
    _ping_at = 0.0

    async def reachable(self) -> bool:
        now = time.monotonic()
        if self._ping_result is None or now - self._ping_at > self.PING_TTL_SECONDS:
            self._ping_result = await self.ping()
            self._ping_at = now
        return self._ping_result


class LLMService(_CachedPing):
    """Claude lewat SDK Anthropic."""

    provider = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: anthropic.AsyncAnthropic | None = None
        if self.settings.anthropic_api_key:
            self._client = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=2,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    def model_for(self, tier: Tier) -> str:
        return self.settings.llm_model_main if tier == "main" else self.settings.llm_model_light

    def _resource_and_kwargs(self, model: str, output_config: dict):
        kwargs: dict = {"output_config": output_config}
        if self.settings.llm_server_fallback and model in SERVER_FALLBACK_MODELS:
            kwargs["betas"] = [SERVER_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
            return self._client.beta.messages, kwargs
        return self._client.messages, kwargs

    async def generate_json[T: BaseModel](
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: dict,
        model_cls: type[T],
        max_tokens: int = 8000,
        effort: Effort = "medium",
        info: LLMCallInfo | None = None,
    ) -> T:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        resource, kwargs = self._resource_and_kwargs(
            model, {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
        )
        try:
            response = await resource.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
        except anthropic.AnthropicError as exc:
            logger.warning("LLM JSON call failed: %s", exc.__class__.__name__)
            raise LLMError("Panggilan LLM gagal") from exc
        _fill_info(info, response)
        if response.stop_reason != "end_turn":
            raise LLMError(f"LLM berhenti dengan stop_reason={response.stop_reason}")
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return model_cls.model_validate_json(text)
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation")
            raise LLMError("Keluaran LLM tidak sesuai skema") from exc

    async def stream_text(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[dict],
        max_tokens: int = 4000,
        effort: Effort = "low",
        info: LLMCallInfo | None = None,
    ) -> AsyncIterator[str]:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        resource, kwargs = self._resource_and_kwargs(model, {"effort": effort})
        try:
            async with resource.stream(
                model=model, max_tokens=max_tokens, system=system, messages=messages, **kwargs
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            logger.warning("LLM stream failed: %s", exc.__class__.__name__)
            raise LLMError("Stream LLM gagal") from exc
        _fill_info(info, final)
        if final.stop_reason == "refusal":
            raise LLMError("LLM menolak permintaan")

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.with_options(timeout=5.0, max_retries=0).models.retrieve(
                self.settings.llm_model_main
            )
            return True
        except anthropic.AnthropicError:
            return False


_GEMINI_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError, TimeoutError)
_GEMINI_STOP = genai_types.FinishReason.STOP
_GEMINI_TRUNCATED = genai_types.FinishReason.MAX_TOKENS


def _gemini_contents(messages: list[dict]) -> list[dict]:
    return [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages
    ]


def _gemini_finish_reason(response):
    candidates = response.candidates or []
    return candidates[0].finish_reason if candidates else None


def _gemini_blocked(response) -> bool:
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason:
        return True
    return _gemini_finish_reason(response) not in (None, _GEMINI_STOP, _GEMINI_TRUNCATED)


def _fill_gemini_info(info: LLMCallInfo | None, response, model: str) -> None:
    if info is None:
        return
    info.model = model
    usage = response.usage_metadata if response is not None else None
    if usage is not None:
        info.input_tokens = usage.prompt_token_count or 0
        info.output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)


class GeminiService(_CachedPing):
    """Gemini lewat SDK resmi Google, dengan antarmuka yang sama seperti LLMService.

    Parameter effort diabaikan: Gemini memakai thinking bawaan tiap model. Error 429 dari
    free tier ditangani seperti kegagalan LLM lain, jadi endpoint turun ke jalur cadangan.
    """

    provider = "gemini"

    def __init__(self, settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: genai.Client | None = None
        if self.settings.gemini_api_key:
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=genai_types.HttpOptions(
                    timeout=int(self.settings.llm_timeout_seconds * 1000),
                    httpx_async_client=http_client,
                ),
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    def model_for(self, tier: Tier) -> str:
        return self.settings.gemini_model_main if tier == "main" else self.settings.gemini_model_light

    async def generate_json[T: BaseModel](
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: dict,
        model_cls: type[T],
        max_tokens: int = 8000,
        effort: Effort = "medium",
        info: LLMCallInfo | None = None,
    ) -> T:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=_gemini_contents([{"role": "user", "content": prompt}]),
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                ),
            )
        except _GEMINI_ERRORS as exc:
            logger.warning("Gemini JSON call failed: %s", exc.__class__.__name__)
            raise LLMError("Panggilan LLM gagal") from exc
        _fill_gemini_info(info, response, model)
        if _gemini_blocked(response) or _gemini_finish_reason(response) != _GEMINI_STOP:
            raise LLMError(f"Gemini berhenti dengan {_gemini_finish_reason(response)}")
        try:
            return model_cls.model_validate_json(response.text or "")
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation")
            raise LLMError("Keluaran LLM tidak sesuai skema") from exc

    async def stream_text(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[dict],
        max_tokens: int = 4000,
        effort: Effort = "low",
        info: LLMCallInfo | None = None,
    ) -> AsyncIterator[str]:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        last = None
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=model,
                contents=_gemini_contents(messages),
                config=genai_types.GenerateContentConfig(
                    system_instruction=system, max_output_tokens=max_tokens
                ),
            )
            async for chunk in stream:
                last = chunk
                if chunk.text:
                    yield chunk.text
        except _GEMINI_ERRORS as exc:
            logger.warning("Gemini stream failed: %s", exc.__class__.__name__)
            raise LLMError("Stream LLM gagal") from exc
        _fill_gemini_info(info, last, model)
        if last is not None and _gemini_blocked(last):
            raise LLMError("LLM menolak permintaan")

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.aio.models.get(model=self.settings.gemini_model_main)
            return True
        except _GEMINI_ERRORS:
            return False


def _groq_reasoning(model: str) -> dict:
    """Sembunyikan proses berpikir model; tanpa ini Qwen menaruh <think> di dalam jawaban."""
    if "gpt-oss" in model:
        return {"include_reasoning": False}
    if "qwen" in model:
        return {"reasoning_format": "hidden"}
    return {}


def _fill_groq_info(info: LLMCallInfo | None, usage, model: str) -> None:
    if info is None:
        return
    info.model = model
    if usage is not None:
        info.input_tokens = usage.prompt_tokens or 0
        info.output_tokens = usage.completion_tokens or 0


class GroqService(_CachedPing):
    """Groq, termasuk model Qwen yang di-host di sana. Antarmuka sama seperti LLMService.

    max_retries=0 supaya kena 429 free tier langsung turun ke jalur cadangan, tidak menunggu.
    Parameter effort diabaikan. generate_json butuh model yang mendukung json_schema strict
    (openai/gpt-oss-*, qwen/*), jadi GROQ_MODEL_MAIN tidak boleh model Llama.
    """

    provider = "groq"

    def __init__(self, settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: groq.AsyncGroq | None = None
        if self.settings.groq_api_key:
            self._client = groq.AsyncGroq(
                api_key=self.settings.groq_api_key,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=0,
                http_client=http_client,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    def model_for(self, tier: Tier) -> str:
        return self.settings.groq_model_main if tier == "main" else self.settings.groq_model_light

    async def generate_json[T: BaseModel](
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: dict,
        model_cls: type[T],
        max_tokens: int = 8000,
        effort: Effort = "medium",
        info: LLMCallInfo | None = None,
    ) -> T:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        try:
            response = await self._client.chat.completions.create(
                model=model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": model_cls.__name__, "strict": True, "schema": schema},
                },
                **_groq_reasoning(model),
            )
        except groq.GroqError as exc:
            logger.warning("Groq JSON call failed: %s", exc.__class__.__name__)
            raise LLMError("Panggilan LLM gagal") from exc
        _fill_groq_info(info, response.usage, model)
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise LLMError(f"Groq berhenti dengan {choice.finish_reason}")
        try:
            return model_cls.model_validate_json(choice.message.content or "")
        except ValidationError as exc:
            logger.warning("LLM output failed schema validation")
            raise LLMError("Keluaran LLM tidak sesuai skema") from exc

    async def stream_text(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[dict],
        max_tokens: int = 4000,
        effort: Effort = "low",
        info: LLMCallInfo | None = None,
    ) -> AsyncIterator[str]:
        if self._client is None:
            raise LLMError("LLM belum dikonfigurasi")
        model = self.model_for(tier)
        usage = None
        finish_reason = None
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, *messages],
                stream=True,
                **_groq_reasoning(model),
            )
            async for chunk in stream:
                if chunk.x_groq is not None and chunk.x_groq.usage is not None:
                    usage = chunk.x_groq.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                if choice.delta.content:
                    yield choice.delta.content
        except groq.GroqError as exc:
            logger.warning("Groq stream failed: %s", exc.__class__.__name__)
            raise LLMError("Stream LLM gagal") from exc
        _fill_groq_info(info, usage, model)
        if finish_reason == "content_filter":
            raise LLMError("LLM menolak permintaan")

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.with_options(timeout=5.0).models.retrieve(self.settings.groq_model_main)
            return True
        except groq.GroqError:
            return False


@lru_cache
def get_llm() -> LLMService | GeminiService | GroqService:
    settings = get_settings()
    services = {"gemini": GeminiService, "groq": GroqService, "anthropic": LLMService}
    return services[settings.llm_provider](settings)
