"""Filter keluaran LLM: larangan kalimat soal PHK dan kebocoran jawaban misi.

Teks diperiksa per kalimat. Untuk stream, SentenceGuard menahan potongan sampai satu
kalimat utuh lalu meloloskannya setelah diperiksa, sehingga kalimat terlarang tidak
pernah sampai ke aplikasi.
"""

import re
from collections.abc import Callable

from app.services.textutil import extract_numbers, normalize, split_sentences

_LAYOFF_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bphk\b",
        r"\bdi-?phk\b",
        r"pemutusan hubungan kerja",
        r"\b(di)?pecat\b",
        r"\bpemecatan\b",
        r"\bdirumahkan\b",
        r"\bdiberhentikan\b",
        r"\blay-?offs?\b",
        r"\bpengurangan (karyawan|pegawai|tenaga kerja)\b",
        r"\bkehilangan (pekerjaan|jabatan|posisi|mata pencaharian)\b",
        r"\b(akan|bisa|dapat|mungkin) (di)?gantikan (oleh |dengan )?(ai|mesin|robot)\b",
        r"\bpekerjaan(mu| anda| kamu)? (akan |bisa )?(hilang|lenyap|punah)\b",
        r"\bposisi(mu| anda| kamu)? (akan |bisa )?(hilang|dihapus|tidak dibutuhkan)\b",
        r"\b(fired|job loss|replaced by ai)\b",
    )
]

LAYOFF_REPLACEMENT = (
    "Fokus kita adalah tugas mana yang bisa dibantu AI dan skill apa yang perlu diperkuat. "
)
LEAK_REPLACEMENT = (
    "Bagian itu perlu kamu kerjakan sendiri. Coba olah dari data simulasi yang tersedia. "
)

_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|persen\b)", re.IGNORECASE)

Check = Callable[[str], str | None]


def mentions_layoff(text: str) -> bool:
    return any(p.search(text) for p in _LAYOFF_PATTERNS)


def layoff_check(sentence: str) -> str | None:
    return LAYOFF_REPLACEMENT if mentions_layoff(sentence) else None


def answer_leak_check(answer_key: list[dict], replacement: str = LEAK_REPLACEMENT) -> Check:
    """Tandai kalimat yang memuat nilai kunci jawaban (angka atau teks persis)."""
    numbers: list[float] = []
    small_numbers: list[float] = []
    phrases: list[str] = []
    for item in answer_key or []:
        # leak_guard=false untuk jawaban yang memang tertulis di data simulasi
        if item.get("leak_guard") is False:
            continue
        expected = item.get("expected")
        kind = item.get("type")
        values = expected if isinstance(expected, list) else [expected]
        for value in values:
            if kind == "number":
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                # Angka kecil bulat (1, 2, 3...) terlalu umum, kecuali ditulis sebagai persen
                if abs(num) >= 10 or num != int(num):
                    numbers.append(num)
                else:
                    small_numbers.append(num)
            elif kind == "exact" and isinstance(value, str) and len(value.strip()) >= 4:
                # Opsi "choice" tidak diperiksa: semua opsinya memang terlihat oleh pengguna
                phrases.append(normalize(value))

    def check(sentence: str) -> str | None:
        if numbers:
            found = extract_numbers(sentence)
            for n in numbers:
                if any(abs(f - n) <= max(1e-6, abs(n) * 1e-9) for f in found):
                    return replacement
        if small_numbers:
            percents = {float(m.replace(",", ".")) for m in _PERCENT.findall(sentence)}
            if percents & set(small_numbers):
                return replacement
        if phrases:
            norm = normalize(sentence)
            if any(p and p in norm for p in phrases):
                return replacement
        return None

    return check


def apply_checks(sentence: str, checks: list[Check]) -> tuple[str, bool]:
    for check in checks:
        replacement = check(sentence)
        if replacement is not None:
            return replacement, True
    return sentence, False


def sanitize(text: str, checks: list[Check] | None = None) -> tuple[str, bool]:
    """Ganti kalimat yang melanggar. Mengembalikan (teks bersih, ada_yang_diganti)."""
    checks = checks or [layoff_check]
    out: list[str] = []
    flagged = False
    for sentence in split_sentences(text):
        clean, hit = apply_checks(sentence, checks)
        flagged |= hit
        if hit and out and out[-1] == clean:
            continue
        out.append(clean)
    return "".join(out), flagged


class SentenceGuard:
    """Menahan potongan stream sampai kalimat utuh, lalu memeriksanya."""

    _BOUNDARY = re.compile(r"[.!?\n]\s+")
    MAX_BUFFER = 600

    def __init__(self, checks: list[Check] | None = None) -> None:
        self.checks = checks or [layoff_check]
        self.buffer = ""
        self.flagged = False
        self._last_replacement: str | None = None

    def _release(self, text: str) -> str:
        out: list[str] = []
        for sentence in split_sentences(text):
            clean, hit = apply_checks(sentence, self.checks)
            if hit:
                self.flagged = True
                if clean == self._last_replacement:
                    continue
                self._last_replacement = clean
            else:
                self._last_replacement = None
            out.append(clean)
        return "".join(out)

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        last = None
        for m in self._BOUNDARY.finditer(self.buffer):
            last = m
        if last is not None:
            ready, self.buffer = self.buffer[: last.end()], self.buffer[last.end() :]
            return self._release(ready)
        if len(self.buffer) > self.MAX_BUFFER:
            cut = self.buffer.rfind(" ")
            if cut > 0:
                ready, self.buffer = self.buffer[: cut + 1], self.buffer[cut + 1 :]
                return self._release(ready)
        return ""

    def flush(self) -> str:
        ready, self.buffer = self.buffer, ""
        return self._release(ready) if ready else ""

    def reset(self) -> None:
        self.buffer = ""
        self._last_replacement = None
