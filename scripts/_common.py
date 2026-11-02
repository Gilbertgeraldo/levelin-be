import csv
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "data" / "demo"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def load_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_csv(path: str | Path, delimiter: str = ",") -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [
            {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh, delimiter=delimiter)
        ]


def split_list(value: str | None, sep: str = ";") -> list[str]:
    return [v.strip() for v in (value or "").split(sep) if v.strip()]
