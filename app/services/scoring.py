"""Aturan skor Levelin, sama dengan prototipe Tim Hearz. Semua angka dihitung di sini, bukan oleh LLM.

- Skor tugas = skor indeks ILO-NASK dari tugas ILO yang cocok (lookup, bukan tebakan).
- Kelompok: >= 0,70 "auto" (Diotomasi), >= 0,40 "assist" (Dibantu AI), selain itu "human"
  (Tetap Manusia). Tugas tanpa padanan ILO = "none", kecuali pengguna memilih kelompoknya sendiri.
- Jam dihemat = jam auto x 0,6 + jam assist x 0,3 (faktor asumsi).
- Skor Kesiapan = porsi jam terpapar (auto + assist) yang sudah siap: penuh bila misi untuk jenis
  pekerjaan tugas itu sudah lulus, setengah bila tugas itu sudah dikerjakan dengan bantuan AI.
"""

import math
from dataclasses import dataclass

AUTO_THRESHOLD = 0.70
ASSIST_THRESHOLD = 0.40

GROUPS = ("auto", "assist", "human", "none")
GROUP_LABELS = {
    "auto": "Diotomasi",
    "assist": "Dibantu AI",
    "human": "Tetap Manusia",
    "none": "Belum terpetakan",
}
EXPOSED = ("auto", "assist")

SAVE_FACTOR = {"auto": 0.6, "assist": 0.3}
READY_WITH_AI = 0.5

# Durasi bawaan bila cerita tidak menyebut berapa lama sebuah tugas dikerjakan
DEFAULT_TASK_HOURS = 2.0


def js_round(x: float) -> int:
    """Pembulatan setengah ke atas seperti Math.round di prototipe (round() Python membulatkan ke genap)."""
    return math.floor(x + 0.5)


def group_for(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= AUTO_THRESHOLD:
        return "auto"
    if score >= ASSIST_THRESHOLD:
        return "assist"
    return "human"


def effective_group(score: float | None, manual_group: str | None) -> str:
    """Kelompok dari skor ILO; tugas tanpa padanan memakai pilihan pengguna bila ada."""
    if score is not None:
        return group_for(score)
    return manual_group if manual_group in ("auto", "assist", "human") else "none"


@dataclass
class ScoredTask:
    hours: float
    group: str
    uses_ai: bool = False
    kind: str | None = None


def _r(x: float, n: int = 2) -> float:
    return round(x, n)


def hours_by_group(tasks: list[ScoredTask]) -> dict[str, float]:
    return {g: _r(sum(t.hours for t in tasks if t.group == g)) for g in GROUPS}


def readiness(tasks: list[ScoredTask], passed_kinds: set[str]) -> int:
    exposed = ready = 0.0
    for t in tasks:
        if t.group not in EXPOSED:
            continue
        exposed += t.hours
        if t.kind and t.kind in passed_kinds:
            ready += t.hours
        elif t.uses_ai:
            ready += READY_WITH_AI * t.hours
    return js_round(100 * ready / exposed) if exposed else 0


def hours_saved(tasks: list[ScoredTask]) -> float:
    return _r(sum(t.hours * SAVE_FACTOR.get(t.group, 0) for t in tasks))


def summarize(tasks: list[ScoredTask], passed_kinds: set[str]) -> dict:
    by_group = hours_by_group(tasks)
    return {
        "readiness_score": readiness(tasks, passed_kinds),
        "hours_by_group": by_group,
        "exposed_hours": _r(sum(by_group[g] for g in EXPOSED)),
        "hours_saved": hours_saved(tasks),
        "save_factor": dict(SAVE_FACTOR),
    }


def share_by_group(by_group: dict[str, float]) -> dict[str, int]:
    """Porsi jam (persen bulat) per kelompok auto/assist/human, tanpa "none"."""
    total = sum(by_group.get(g, 0) for g in ("auto", "assist", "human"))
    if not total:
        return {"auto": 0, "assist": 0, "human": 0}
    raw = {g: 100 * by_group.get(g, 0) / total for g in ("auto", "assist", "human")}
    shares = {g: int(v) for g, v in raw.items()}
    # Bagikan sisa pembulatan ke porsi dengan pecahan terbesar supaya totalnya tepat 100
    for g in sorted(raw, key=lambda g: raw[g] - shares[g], reverse=True)[: 100 - sum(shares.values())]:
        shares[g] += 1
    return shares
