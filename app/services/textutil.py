import re
import unicodedata

_NUMBER_TOKEN = re.compile(r"(-?\d[\d.,]*)(?:\s*(ribu|rb|juta|jt|miliar|milyar|triliun)\b)?", re.IGNORECASE)
_UNITS = {
    "ribu": 1e3,
    "rb": 1e3,
    "juta": 1e6,
    "jt": 1e6,
    "miliar": 1e9,
    "milyar": 1e9,
    "triliun": 1e12,
}


def normalize(text: str) -> str:
    """Huruf kecil, tanpa aksen/tanda baca, spasi dirapikan. Untuk perbandingan teks longgar."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^\w\s%]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def number_candidates(token: str) -> set[float]:
    """Tafsiran angka dari satu token, mendukung format Indonesia (1.250.000,5) dan Inggris (1,250,000.5)."""
    token = token.strip(".,")
    out: set[float] = set()
    for cleaned in (
        token.replace(".", "").replace(",", "."),  # format Indonesia
        token.replace(",", ""),  # format Inggris
    ):
        try:
            out.add(float(cleaned))
        except ValueError:
            pass
    return out


def extract_numbers(text: str) -> set[float]:
    found: set[float] = set()
    for token, unit in _NUMBER_TOKEN.findall(text or ""):
        values = number_candidates(token)
        found |= values
        if unit:
            found |= {v * _UNITS[unit.lower()] for v in values}
    return found


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " ".join(to_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(to_text(v) for v in value.values())
    return str(value)


def split_sentences(text: str) -> list[str]:
    """Pisah per kalimat dengan tetap menyimpan spasi/garis baru di ujungnya."""
    parts = re.split(r"(?<=[.!?\n])(\s+)", text)
    sentences: list[str] = []
    for i in range(0, len(parts), 2):
        sentence = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if sentence:
            sentences.append(sentence)
    return sentences


def chunk_words(text: str, size: int = 6) -> list[str]:
    """Potong teks menjadi beberapa kata per potong untuk di-stream sebagai SSE."""
    tokens = re.findall(r"\S+\s*", text)
    return ["".join(tokens[i : i + size]) for i in range(0, len(tokens), size)]


def fmt_num(value: float | int | None) -> str:
    """Format angka gaya Indonesia: 12,5 dan 1.250 (tanpa desimal bila bulat)."""
    if value is None:
        return "0"
    value = round(float(value), 1)
    whole, _, frac = f"{value:,.1f}".partition(".")
    whole = whole.replace(",", ".")
    return whole if frac == "0" else f"{whole},{frac}"
