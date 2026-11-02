"""Protokol SSE untuk semua endpoint streaming.

Event yang dikirim:
- meta   {"engine": "llm" | "fallback"}   sebelum teks pertama
- delta  {"text": "..."}                    potongan teks (sudah lolos guardrail)
- reset  {"reason": "..."}                  buang teks parsial; LLM gagal di tengah jalan
- done   {"engine": ..., ...extra}          selesai
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi.sse import ServerSentEvent

from app.services.guardrails import SentenceGuard, sanitize
from app.services.llm import LLMError
from app.services.textutil import chunk_words

logger = logging.getLogger("levelin.stream")


def sse(event: str, data: dict) -> ServerSentEvent:
    return ServerSentEvent(event=event, data=data)


async def stream_with_fallback(
    *,
    llm_stream: Callable[[], AsyncIterator[str]] | None,
    fallback_text: Callable[[], str],
    guard: SentenceGuard,
    on_complete: Callable[[str, str, bool], Awaitable[dict | None]] | None = None,
) -> AsyncIterator[ServerSentEvent]:
    """Stream teks LLM lewat guardrail; bila gagal, stream teks cadangan berbasis aturan.

    on_complete(full_text, engine, flagged) dipanggil sekali di akhir (mis. simpan pesan,
    catat pemakaian AI) dan boleh mengembalikan data tambahan untuk event done.
    """
    parts: list[str] = []
    engine = "fallback"
    started = False

    if llm_stream is not None:
        engine = "llm"
        try:
            async for chunk in llm_stream():
                out = guard.feed(chunk)
                if out:
                    if not started:
                        yield sse("meta", {"engine": "llm"})
                        started = True
                    parts.append(out)
                    yield sse("delta", {"text": out})
            tail = guard.flush()
            if tail:
                if not started:
                    yield sse("meta", {"engine": "llm"})
                    started = True
                parts.append(tail)
                yield sse("delta", {"text": tail})
            if not "".join(parts).strip():
                raise LLMError("LLM tidak mengembalikan teks")
        except LLMError as exc:
            logger.info("Stream falling back to rules: %s", exc)
            engine = "fallback"
            guard.reset()
            if started:
                yield sse("reset", {"reason": "llm_error"})
            parts = []

    if engine == "fallback":
        yield sse("meta", {"engine": "fallback"})
        text, flagged = sanitize(fallback_text(), guard.checks)
        guard.flagged |= flagged
        for piece in chunk_words(text):
            parts.append(piece)
            yield sse("delta", {"text": piece})

    extra = None
    if on_complete is not None:
        extra = await on_complete("".join(parts), engine, guard.flagged)
    yield sse("done", {"engine": engine, **(extra or {})})
