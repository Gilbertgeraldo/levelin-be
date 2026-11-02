# Levelin Backend

FastAPI untuk aplikasi pekerja (Flutter) dan dashboard HR (web), sesuai *Rancangan endpoint backend v0.2*.
Semua endpoint di daftar lengkap rancangan sudah ada (61 operasi). Dokumentasi interaktif ada di `/docs`.

```
Flutter / Web HR ──JWT Supabase──▶ FastAPI ──▶ Gemini, Groq, atau Claude (ekstraksi, atasan AI, rubrik, penjelasan)
                                     │     └──▶ Voyage (embedding), cadangan: embedding hashing lokal
                                     └──▶ Postgres + pgvector (Supabase)
```

## Menjalankan lokal

```bash
uv sync
cp .env.example .env            # isi SUPABASE_JWT_SECRET minimal
uv run alembic upgrade head
uv run python -m scripts.seed_demo --sample-workers 15
uv run fastapi dev main.py      # http://localhost:8000/docs
```

Default `.env.example` memakai SQLite supaya bisa langsung jalan. Untuk Supabase, isi `DATABASE_URL` dengan
connection string Postgres (format `postgresql://...` otomatis diubah ke driver asyncpg; untuk pooler port 6543
set `DB_PGBOUNCER=true`). Migrasi mengaktifkan `vector` dan, bila tersedia, `pg_trgm`.

Tanpa key penyedia yang dipilih di `LLM_PROVIDER` (`GEMINI_API_KEY`, `GROQ_API_KEY`, atau `ANTHROPIC_API_KEY`) dan `VOYAGE_API_KEY`,
semua fitur tetap jalan lewat cadangan berbasis aturan, dan responsnya ditandai `engine: "fallback"`.
Cek penyedia yang aktif di `GET /health`.

### Token untuk mencoba

Di produksi aplikasi login langsung ke Supabase Auth dan mengirim access token-nya. Untuk dev:

```bash
uv run python -m scripts.dev_token --email budi@contoh-sejahtera.test --name "Budi Santoso"
uv run python -m scripts.dev_token --email hr@contoh-sejahtera.test --role hr_admin
uv run python -m scripts.dev_token --email tim@levelin.test --role reviewer
```

Kedua email `contoh-sejahtera.test` punya undangan di data demo, jadi otomatis masuk organisasi
"PT Contoh Sejahtera" saat request pertama.

## Keputusan yang diambil

Bagian "Yang perlu diputuskan" di rancangan belum ada di dokumen, jadi ini pilihan default yang dipakai.
Semuanya bisa diubah lewat konfigurasi atau satu konstanta.

| Topik | Pilihan |
|---|---|
| Login | Aplikasi login ke Supabase Auth; FastAPI hanya memverifikasi JWT (HS256 lewat `SUPABASE_JWT_SECRET`, atau RS256/ES256 lewat JWKS dari `SUPABASE_URL`). Pengguna dibuat otomatis saat request pertama. |
| Peran | Dibaca dari `app_metadata.role` di JWT (hanya bisa diisi service role Supabase), default `worker`. Undangan HR dengan `role: hr_admin` juga memberi peran itu. |
| Masuk organisasi | Lewat undangan HR: saat pengguna dengan email yang diundang login, ia otomatis bergabung ke organisasi dan departemennya. |
| Model LLM | Default Gemini: `gemini-3.8-flash` untuk ekstraksi tugas, atasan AI, dan rubrik; `gemini-3.5-flash-lite` untuk penjelasan peta dan narasi rencana ("LLM ringan"). Alternatif gratis: `LLM_PROVIDER=groq` dengan `openai/gpt-oss-120b` dan `llama-3.1-8b-instant`; Qwen dipakai lewat `GROQ_MODEL_MAIN=qwen/qwen3.8-27b`. Free tier hanya untuk demo: kuotanya dipakai bersama, dan data sebaiknya bukan data pengguna asli. Untuk produksi set `LLM_PROVIDER=anthropic` (`claude-opus-5-5` dan `claude-haiku-5-5`) atau pakai paket berbayar. |
| Embedding | Voyage `voyage-3.5`, 1024 dimensi. Cadangan: embedding hashing n-gram lokal. |
| Pembayaran | Midtrans Snap; webhook diverifikasi dengan signature SHA-512. |
| Persetujuan data | `POST /v1/audits` menolak (403 `consent_required`) bila profil belum menyetujui pemrosesan data. |
| Validasi | Error validasi memakai 400 `validation_error` (bukan 422) sesuai daftar status di rancangan. |

## Aturan yang dijaga backend

**Skor di server.** Rumus ada di [app/services/scoring.py](app/services/scoring.py):

- Skor tugas = skor ILO dari template tugas yang paling mirip (kosinus embedding). LLM hanya memecah cerita jadi daftar tugas; angkanya tidak pernah dari LLM.
- Kelompok: `>= 0,70` high, `>= 0,40` medium, sisanya low.
- Jam dihemat per minggu = Σ jam × porsi hemat kelompok (high 50%, medium 25%, low 0%). Ini asumsi; ubah `SAVING_RATE`.
- Skor Kesiapan (0-100) = 100 × (0,5 × adopsi AI di tugas terpapar + 0,3 × (1 − indeks paparan) + 0,2 × min(1, skill terbukti / 5)).

**Kunci jawaban tidak keluar server.** `GET /v1/missions/{id}` tidak memuat `answer_key`; cek objektif jalan di
[app/services/grading.py](app/services/grading.py) saat submit. Atasan AI tidak menerima kunci jawaban, dan
setiap kalimat balasannya dicek: kalimat yang memuat nilai jawaban (angka/teks persis) diganti sebelum dikirim.
Skor Kesiapan di audit terbaru dihitung ulang setiap skill terbukti berubah.

**HR hanya agregat.** Dashboard dan laporan menyembunyikan angka kelompok yang kurang dari `K_ANONYMITY_MIN`
(default 5) orang. Departemen kecil digabung jadi "Lainnya"; bila "Lainnya" masih di bawah ambang, departemen
terkecil ikut digabung supaya angkanya tidak bisa dihitung dari selisih total. Kandidat internal hanya pekerja yang menyalakan
"Bagikan ke HR", dan isinya skill terbukti, bukan audit.

**Endpoint AI punya cadangan.** Keluaran JSON LLM memakai structured output lalu divalidasi Pydantic; gagal
validasi, ditolak, atau error API berarti cadangan berbasis aturan dipakai. Penilaian cadangan selalu masuk
antrean review manusia; penilaian LLM disampel `REVIEW_SAMPLE_RATE`. Rate limit per pengguna dicatat di tabel
`ai_usage` (konsisten walau server lebih dari satu instance) dan dibalas `429` dengan header `Retry-After`.

**Guardrail teks.** Prompt melarang topik PHK dan atasan AI dilarang mengerjakan tugas. Keluaran juga difilter
per kalimat ([app/services/guardrails.py](app/services/guardrails.py)), termasuk saat streaming dan pada umpan
balik rubrik. Filter jawaban mengenali angka (`162.000.000`, `162 juta`, `8 persen`) dan teks persis. Jawaban
pilihan seperti "Timur" tidak bisa difilter karena semua opsinya memang terlihat, jadi hanya dicegah oleh prompt.

## Streaming (SSE)

`POST /v1/audits/{id}/explain`, `POST /v1/plans/{id}/narrative`, dan `POST /v1/attempts/{id}/messages`
membalas `text/event-stream`:

```
event: meta    data: {"engine": "llm"}          sebelum teks pertama
event: delta   data: {"text": "Skor Kesiapan..."}  potongan teks, gabungkan berurutan
event: reset   data: {"reason": "llm_error"}     buang teks yang sudah tampil; cadangan menyusul
event: done    data: {"engine": "llm", "message_id": "..."}
```

Rate limit dan kepemilikan dicek sebelum stream dibuka, jadi error tetap berupa JSON biasa (401/404/429).
Karena memakai POST, gunakan klien SSE yang mendukung POST (di Flutter: `http` dengan stream response,
di web: `fetch` + `ReadableStream`).

## Paginasi dan cache

Endpoint daftar menerima `?page=&limit=` (maks 100) dan membalas `{"data": [...], "meta": {page, limit, total,
total_pages, has_next}}`. Data referensi (`/v1/occupations*`, `/v1/skills`) mengirim `Cache-Control: public, max-age=3600`.

## Data referensi asli

Data di `data/demo/` hanya untuk demo; skor ILO di dalamnya ilustratif, bukan angka resmi. Impor data asli:

```bash
uv run python -m scripts.ingest_kbji data/kbji.csv                 # kbji_code,title,isco_code,aliases
uv run python -m scripts.ingest_ilo data/ilo_tasks.csv --isco-col isco08 --task-col task --score-col score \
    --translation data/ilo_tasks_id.csv --occupations data/ilo_occupations.csv
uv run python -m scripts.ingest_esco --skills skills_en.csv --occupations occupations_en.csv \
    --relations occupationSkillRelations_en.csv --labels-id skills_id.csv
uv run python -m scripts.ingest_courses data/courses.csv
uv run python -m scripts.ingest_missions data/missions.json
uv run python -m scripts.embed_reference
```

Format kolom tiap berkas ada di docstring skripnya.

## Tugas terjadwal

`uv run python -m scripts.send_reminders` (harian): pengingat cek ulang Peta Tugas setiap `REMINDER_DAYS` hari
dan pengingat misi untuk rencana yang mandek 7 hari. Notifikasi disimpan in-app dan dikirim lewat FCM bila
`FCM_SERVICE_ACCOUNT_FILE` diisi.

## Tes

```bash
uv run pytest                                            # SQLite in-memory
TEST_DATABASE_URL=postgresql+asyncpg://.../levelin_test uv run pytest   # Postgres + pgvector
```

Tes mencakup alur pekerja L1-L10, jalur LLM dengan LLM palsu (validasi skema, filter PHK di stream,
reset saat stream putus, bocoran jawaban di chat), bentuk request ke SDK Google, Groq, dan Anthropic, dashboard HR
k-anonim, peninjau, dan webhook pembayaran.

## Struktur

```
app/
  api/v1/        router per bagian rancangan (me, reference, audits, plans, courses, missions,
                 attempts, passport, jobs, org, notifications, billing, admin)
  services/      logika: skor, audit, LLM, embedding, guardrail, penilaian, rencana, HR, pembayaran
  models/        tabel SQLAlchemy (30 tabel)
  schemas/       kontrak request/response
  core/          konfigurasi, auth JWT, format error, paginasi, rate limit
alembic/         migrasi database
scripts/         seed demo, impor data, embedding, pengingat, token dev
data/demo/       data demo (jabatan, tugas, skill, kelas, misi, lowongan)
```

## Deploy

`Dockerfile` menjalankan migrasi lalu uvicorn di `$PORT`. Set variabel `.env` di platform hosting.
Untuk lebih dari satu replika, jalankan `alembic upgrade head` sebagai langkah release terpisah.
Isi `SUPABASE_SERVICE_ROLE_KEY` di produksi: tanpa itu `DELETE /v1/me` hanya menghapus data di backend,
bukan akun Supabase Auth.
