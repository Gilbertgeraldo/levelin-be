"""Aturan skor Levelin. Semua angka yang dilihat pengguna dihitung di sini, bukan oleh LLM.

- Skor tugas = skor paparan ILO dari template tugas yang cocok (lookup, bukan tebakan).
- Kelompok: >= 0,70 "high", >= 0,40 "medium", selain itu "low".
- Jam dihemat dan Skor Kesiapan memakai konstanta asumsi di bawah; ubah di satu tempat ini.
"""

from dataclasses import dataclass

HIGH_THRESHOLD = 0.70
MEDIUM_THRESHOLD = 0.40

GROUP_LABELS = {
    "high": "Bisa banyak dibantu AI",
    "medium": "AI jadi rekan kerja",
    "low": "Tetap keunggulan manusia",
}

# Asumsi porsi jam tugas yang bisa dihemat bila dibantu AI, per kelompok
SAVING_RATE = {"high": 0.50, "medium": 0.25, "low": 0.0}

# Bobot komponen Skor Kesiapan (total 1,0)
READINESS_WEIGHTS = {"adoption": 0.5, "human_share": 0.3, "skill_proof": 0.2}
# Jumlah skill terbukti di Paspor yang dianggap penuh untuk komponen skill_proof
SKILL_PROOF_TARGET = 5

# Asumsi jam kerja per minggu saat cerita tidak menyebut durasi
DEFAULT_WEEKLY_HOURS = 40.0


def group_for(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


@dataclass
class ScoredTask:
    hours: float
    score: float
    uses_ai: bool
    description: str = ""

    @property
    def group(self) -> str:
        return group_for(self.score)


def _r(x: float, n: int = 2) -> float:
    return round(x, n)


def summarize(tasks: list[ScoredTask], proven_skills: int) -> dict:
    total_hours = sum(t.hours for t in tasks)
    exposure = sum(t.hours * t.score for t in tasks) / total_hours if total_hours else 0.0

    groups = {}
    for key, label in GROUP_LABELS.items():
        members = [t for t in tasks if t.group == key]
        hours = sum(t.hours for t in members)
        groups[key] = {
            "label": label,
            "task_count": len(members),
            "hours": _r(hours, 1),
            "share": _r(hours / total_hours, 3) if total_hours else 0.0,
        }

    exposed = [t for t in tasks if t.group in ("high", "medium")]
    exposed_hours = sum(t.hours for t in exposed)
    adopted_hours = sum(t.hours for t in exposed if t.uses_ai)
    adoption = adopted_hours / exposed_hours if exposed_hours else 1.0

    saved_potential = sum(t.hours * SAVING_RATE[t.group] for t in tasks)
    saved_now = sum(t.hours * SAVING_RATE[t.group] for t in tasks if t.uses_ai)

    skill_proof = min(1.0, proven_skills / SKILL_PROOF_TARGET)
    components = {
        "adoption": _r(adoption, 3),
        "human_share": _r(1 - exposure, 3),
        "skill_proof": _r(skill_proof, 3),
    }
    readiness = round(100 * sum(READINESS_WEIGHTS[k] * v for k, v in components.items()))

    return {
        "total_hours": _r(total_hours, 1),
        "exposure_index": _r(exposure, 3),
        "groups": groups,
        "ai_adoption": _r(adoption, 3),
        "hours_saved_week": _r(saved_potential, 1),
        "hours_saved_now": _r(saved_now, 1),
        "readiness_score": max(0, min(100, readiness)),
        "readiness_components": components,
        "readiness_weights": READINESS_WEIGHTS,
        "thresholds": {"high": HIGH_THRESHOLD, "medium": MEDIUM_THRESHOLD},
    }


def fill_missing_hours(hours: list[float | None]) -> list[float]:
    """Bagi sisa jam mingguan rata ke tugas yang belum punya durasi (min 1 jam, kelipatan 0,5)."""
    missing = [i for i, h in enumerate(hours) if h is None]
    if not missing:
        return [float(h) for h in hours]
    known = sum(h for h in hours if h is not None)
    remaining = max(DEFAULT_WEEKLY_HOURS - known, 2.0 * len(missing))
    each = max(1.0, round(remaining / len(missing) * 2) / 2)
    return [float(h) if h is not None else each for h in hours]
