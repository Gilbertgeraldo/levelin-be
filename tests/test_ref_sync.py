"""Tes sinkronisasi schema `ref` (ESCO, KBJI 2014, Lightcast) dan endpoint referensi."""

import json
import pytest
from sqlalchemy import select, text

from app import ingest
from app.models import Occupation, Skill

pytestmark = pytest.mark.anyio


async def _setup_ref_schema(db, *, with_kbji: bool = False) -> None:
    """Buat schema `ref` sementara di koneksi uji (SQLite ATTACH `:memory:` AS ref)."""
    await db.execute(text("ATTACH DATABASE ':memory:' AS ref"))
    await db.execute(
        text(
            "CREATE TABLE ref.esco_isco_groups ("
            "uri TEXT PRIMARY KEY, code TEXT NOT NULL, label TEXT NOT NULL, "
            "alt_labels TEXT, description TEXT)"
        )
    )
    await db.execute(
        text(
            "CREATE TABLE ref.esco_occupations ("
            "uri TEXT PRIMARY KEY, code TEXT, isco_group TEXT, label TEXT NOT NULL, "
            "alt_labels TEXT, description TEXT, definition TEXT)"
        )
    )
    await db.execute(
        text(
            "CREATE TABLE ref.esco_skills ("
            "uri TEXT PRIMARY KEY, skill_type TEXT, reuse_level TEXT, "
            "label TEXT NOT NULL, alt_labels TEXT, description TEXT)"
        )
    )
    await db.execute(
        text(
            "CREATE TABLE ref.esco_occupation_skills ("
            "occupation_uri TEXT NOT NULL, skill_uri TEXT NOT NULL, "
            "relation_type TEXT, skill_type TEXT)"
        )
    )
    await db.execute(
        text(
            "CREATE TABLE ref.kbji ("
            "code TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, "
            "level SMALLINT NOT NULL, isco4 TEXT)"
        )
    )
    await db.execute(
        text(
            "CREATE TABLE ref.lightcast_skills ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, type_name TEXT, "
            "category TEXT, subcategory TEXT, description TEXT, info_url TEXT, raw TEXT)"
        )
    )

    # Grup ISCO 4 digit
    await db.execute(
        text(
            "INSERT INTO ref.esco_isco_groups (uri, code, label, alt_labels, description) VALUES "
            "('http://data.europa.eu/esco/isco/C2411', '2411', 'Accountants', '[]', 'Accountants group'), "
            "('http://data.europa.eu/esco/isco/C3313', '3313', 'Accounting associate professionals', '[]', NULL)"
        )
    )

    # Okupasi ESCO
    occs = [
        (
            "http://data.europa.eu/esco/occupation/occ-accountant",
            "2411.1",
            "2411",
            "accountant",
            json.dumps(["certified public accountant", "production accountant", "tax accountant"]),
            "Accountants review financial records and prepare tax returns.",
        ),
        (
            "http://data.europa.eu/esco/occupation/occ-acc-analyst",
            "2411.1.1",
            "2411",
            "accounting analyst",
            json.dumps(["accounts analyst", "accounting systems analyst"]),
            "Accounting analysts examine financial statements.",
        ),
        (
            "http://data.europa.eu/esco/occupation/occ-acc-assistant",
            "3313.1",
            "3313",
            "accounting assistant",
            json.dumps(["assistant accountant", "billing and accounting assistant"]),
            "Accounting assistants support accountants with bookkeeping.",
        ),
    ]
    for uri, code, isco, label, alts, desc in occs:
        await db.execute(
            text(
                "INSERT INTO ref.esco_occupations (uri, code, isco_group, label, alt_labels, description) "
                "VALUES (:uri, :code, :isco, :label, :alts, :desc)"
            ),
            {"uri": uri, "code": code, "isco": isco, "label": label, "alts": alts, "desc": desc},
        )

    # Skill ESCO (sebagian besar kolom opsional NULL sesuai kondisi nyata)
    skills = [
        ("http://data.europa.eu/esco/skill/s-ledger", None, None, "maintain general ledger", "[]", None),
        ("http://data.europa.eu/esco/skill/s-tax", None, None, "prepare tax returns", "[]", None),
        ("http://data.europa.eu/esco/skill/s-fin-stmt", None, None, "analyse financial statements", "[]", None),
        ("http://data.europa.eu/esco/skill/s-budget", None, None, "manage budgets", "[]", None),
        (
            "http://data.europa.eu/esco/skill/s-comm",
            "skill",
            "transversal skills and competences",
            "communicate financial information",
            json.dumps(["financial communication"]),
            "Explain financial data to stakeholders.",
        ),
    ]
    for uri, st, rl, label, alts, desc in skills:
        await db.execute(
            text(
                "INSERT INTO ref.esco_skills (uri, skill_type, reuse_level, label, alt_labels, description) "
                "VALUES (:uri, :st, :rl, :label, :alts, :desc)"
            ),
            {"uri": uri, "st": st, "rl": rl, "label": label, "alts": alts, "desc": desc},
        )

    # Relasi skill per okupasi ESCO
    rels = [
        # accountant (2411.1): ledger (essential), tax (essential), fin-stmt (optional)
        ("http://data.europa.eu/esco/occupation/occ-accountant", "http://data.europa.eu/esco/skill/s-ledger", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-accountant", "http://data.europa.eu/esco/skill/s-tax", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-accountant", "http://data.europa.eu/esco/skill/s-fin-stmt", "optional"),
        # accounting analyst (2411.1.1): ledger (essential), fin-stmt (essential), budget (essential), comm (optional)
        ("http://data.europa.eu/esco/occupation/occ-acc-analyst", "http://data.europa.eu/esco/skill/s-ledger", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-acc-analyst", "http://data.europa.eu/esco/skill/s-fin-stmt", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-acc-analyst", "http://data.europa.eu/esco/skill/s-budget", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-acc-analyst", "http://data.europa.eu/esco/skill/s-comm", "optional"),
        # accounting assistant (3313.1): ledger (essential), tax (optional)
        ("http://data.europa.eu/esco/occupation/occ-acc-assistant", "http://data.europa.eu/esco/skill/s-ledger", "essential"),
        ("http://data.europa.eu/esco/occupation/occ-acc-assistant", "http://data.europa.eu/esco/skill/s-tax", "optional"),
    ]
    for occ_uri, skill_uri, rel_type in rels:
        await db.execute(
            text(
                "INSERT INTO ref.esco_occupation_skills (occupation_uri, skill_uri, relation_type) "
                "VALUES (:occ_uri, :skill_uri, :rel_type)"
            ),
            {"occ_uri": occ_uri, "skill_uri": skill_uri, "rel_type": rel_type},
        )

    if with_kbji:
        await db.execute(
            text(
                "INSERT INTO ref.kbji (code, title, description, level, isco4) VALUES "
                "('241101', 'Akuntan Publik Terdaftar', 'Pemeriksa laporan keuangan', 6, '2411')"
            )
        )


async def test_sync_ref_and_endpoints_without_kbji(client, sessionmaker):
    async with sessionmaker() as db:
        await _setup_ref_schema(db, with_kbji=False)
        stats1 = await ingest.sync_ref(db)
        await db.commit()
        assert stats1["esco_occupations"] == 3
        assert stats1["kbji_occupations"] == 0
        assert stats1["occupations_inserted"] == 3
        assert stats1["skills_inserted"] == 5
        assert stats1["occupation_skill_links"] == 9

        # Jalankan ulang (idempoten): tidak menambah duplikat dan tidak menghapus data lama
        stats2 = await ingest.sync_ref(db)
        await db.commit()
        assert stats2["occupations_inserted"] == 0
        assert stats2["occupations_updated"] == 0
        assert stats2["skills_inserted"] == 0
        assert stats2["skills_updated"] == 0

    # 1. GET /v1/occupations?q=account (cocok label Inggris)
    r = await client.get("/v1/occupations", params={"q": "account"})
    assert r.status_code == 200
    codes = [o["code"] for o in r.json()]
    assert "2411.1" in codes and "2411.1.1" in codes and "3313.1" in codes

    # Cocok dari alt_labels ("certified public accountant")
    r_alt = await client.get("/v1/occupations", params={"q": "certified public"})
    assert r_alt.status_code == 200
    assert r_alt.json()[0]["code"] == "2411.1"

    # 2. GET /v1/occupations/{code} & /tasks (mewarisi paparan & tugas grup ISCO 3313)
    r_detail = await client.get("/v1/occupations/3313.1")
    assert r_detail.status_code == 200
    body = r_detail.json()
    assert body["code"] == "3313.1" and body["isco_code"] == "3313"
    assert body["title"] == "accounting assistant" and body["title_en"] == "accounting assistant"
    assert "assistant accountant" in body["aliases"]
    assert body["avg_exposure"] == 0.49
    assert body["task_count"] > 0

    r_tasks = await client.get("/v1/occupations/3313.1/tasks")
    assert r_tasks.status_code == 200
    assert len(r_tasks.json()["tasks"]) == body["task_count"]

    # 3. GET /v1/occupations/{code}/next-roles (Jaccard berbobot atas skill ESCO)
    r_next = await client.get("/v1/occupations/2411.1/next-roles")
    assert r_next.status_code == 200
    next_roles = r_next.json()
    assert len(next_roles) >= 2
    by_code = {item["occupation"]["code"]: item for item in next_roles}
    assert "3313.1" in by_code and "2411.1.1" in by_code
    assert by_code["2411.1.1"]["direction"] == "mirip"
    assert by_code["2411.1.1"]["similarity"] > 0
    # skills_to_learn untuk 2411.1 -> 2411.1.1 memprioritaskan essential yang belum dimiliki (manage budgets)
    learn_labels = [s["label"] for s in by_code["2411.1.1"]["skills_to_learn"]]
    assert "manage budgets" in learn_labels

    # 4. GET /v1/skills
    r_skills = await client.get("/v1/skills", params={"q": "ledger"})
    assert r_skills.status_code == 200
    assert r_skills.json()["data"][0]["label"] == "maintain general ledger"

    r_trans = await client.get("/v1/skills", params={"category": "transversal"})
    assert any(s["label"] == "communicate financial information" for s in r_trans.json()["data"])

    # 5. Kolom terjemahan label_id dipakai bila terisi, dan dipertahankan saat sync_ref dijalankan ulang
    async with sessionmaker() as db:
        occ = await db.get(Occupation, "2411.1")
        occ.label_id = "Akuntan"
        skill = await db.scalar(
            select(Skill).where(Skill.esco_uri == "http://data.europa.eu/esco/skill/s-ledger")
        )
        skill.label_id = "Mengelola buku besar"
        await ingest.sync_ref(db)
        await db.commit()

    r_id = await client.get("/v1/occupations/2411.1")
    assert r_id.json()["title"] == "Akuntan" and r_id.json()["title_en"] == "accountant"
    r_skill_id = await client.get("/v1/skills", params={"q": "buku besar"})
    assert r_skill_id.json()["data"][0]["label"] == "Mengelola buku besar"
    assert r_skill_id.json()["data"][0]["label_en"] == "maintain general ledger"


async def test_sync_ref_with_kbji_populated(client, sessionmaker):
    async with sessionmaker() as db:
        await _setup_ref_schema(db, with_kbji=True)
        stats = await ingest.sync_ref(db)
        await db.commit()
        assert stats["kbji_occupations"] == 1

    # Pencarian judul KBJI menemukan jabatan KBJI sekaligus memperkaya alias jabatan ESCO se-ISCO
    r = await client.get("/v1/occupations", params={"q": "Akuntan Publik Terdaftar"})
    assert r.status_code == 200
    codes = [o["code"] for o in r.json()]
    assert "241101" in codes
    assert "2411.1" in codes
