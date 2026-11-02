"""Embedding untuk mencocokkan tugas cerita ke template tugas ILO dan pencarian jabatan.

Penyedia utama Voyage AI. Cadangannya embedding hashing lokal (n-gram karakter), cukup
untuk kecocokan leksikal ketika API embedding tidak tersedia. Vektor dari model berbeda
tidak boleh dibandingkan, jadi setiap hasil membawa nama modelnya.
"""

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import httpx

from app.core.config import Settings, get_settings
from app.services.textutil import normalize

logger = logging.getLogger("levelin.embeddings")

InputType = Literal["query", "document"]
HASHING_MODEL = "hashing-v1"


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class HashingEmbedder:
    name = HASHING_MODEL

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _features(self, text: str) -> list[tuple[str, float]]:
        words = [w for w in re.findall(r"\w+", normalize(text)) if len(w) > 1]
        feats: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        for w in words:
            padded = f" {w} "
            feats += [(f"c:{padded[i : i + 3]}", 0.5) for i in range(len(padded) - 2)]
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for feat, weight in self._features(text):
                digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign * weight
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"
    BATCH = 128

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.api_key = api_key
        self.name = model
        self.dim = dim

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for start in range(0, len(texts), self.BATCH):
                batch = texts[start : start + self.BATCH]
                resp = await client.post(
                    self.URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "input": batch,
                        "model": self.name,
                        "input_type": input_type,
                        "output_dimension": self.dim,
                    },
                )
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                out += [d["embedding"] for d in data]
        return out


class EmbeddingService:
    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.settings = settings
        self.fallback = HashingEmbedder(settings.embedding_dim)
        self.primary: VoyageEmbedder | None = None
        if settings.embedding_provider == "voyage" and settings.voyage_api_key:
            self.primary = VoyageEmbedder(
                settings.voyage_api_key, settings.embedding_model, settings.embedding_dim
            )

    @property
    def primary_model(self) -> str:
        return self.primary.name if self.primary else self.fallback.name

    def min_similarity(self, model: str) -> float:
        # Skala kemiripan hashing lebih rendah daripada embedding semantik
        return 0.20 if model == HASHING_MODEL else self.settings.match_min_similarity

    async def embed(
        self, texts: list[str], input_type: InputType = "document", model: str | None = None
    ) -> EmbeddingResult:
        """Embed dengan model tertentu (bila diminta) atau penyedia utama, cadangan hashing."""
        if not texts:
            return EmbeddingResult([], model or self.primary_model)
        use_primary = self.primary is not None and model in (None, self.primary.name)
        if use_primary:
            try:
                return EmbeddingResult(await self.primary.embed(texts, input_type), self.primary.name)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                logger.warning("Primary embedding failed, using hashing fallback: %s", exc)
        return EmbeddingResult(self.fallback.embed(texts), self.fallback.name)


@lru_cache
def get_embeddings() -> EmbeddingService:
    return EmbeddingService()
