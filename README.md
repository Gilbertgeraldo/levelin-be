# Levelin Backend

FastAPI untuk aplikasi pekerja (Flutter) dan web dashboard HR & atasan tim, sesuai *Rancangan endpoint backend
v0.2*, payload v0.1 (`API_ENDPOINTS.md`), dan prototipe `Levelin.html` Tim Hearz. Dokumentasi interaktif ada di
`/docs`; kontrak mesin di `/openapi.json`.

```
Flutter (pekerja) ─┐                ┌─▶ Gemini, Groq, atau Claude (ekstraksi, atasan AI, rubrik, penjelasan)
                   ├─JWT Supabase─▶ FastAPI ─┼─▶ Voyage (embedding), cadangan: embedding hashing lokal
Web (HR, atasan) ──┘                └─▶ Postgres + pgvector (Supabase)
```

## Menjalankan lokal

```bash
uv sync
cp .env.example .env            # isi SUPABASE_JWT_SECRET minimal
uv run alembic upgrade head
uv run python -m scripts.seed_demo --sample-workers
uv run fastapi dev main.py      # http://localhost:8000/docs
```

Default `.env.example` memakai SQLite supaya bisa langsung jalan. Untuk Supabase, isi `DATABASE_URL` dengan
connection string Postgres (format `postgresql://...` otomatis diubah ke driver asyncpg; untuk pooler port 6543
set `DB_PGBOUNCER=true`). Migrasi mengaktifkan `vector` dan, bila tersedia, `pg_trgm`.

Tanpa key penyedia yang dipilih di `LLM_PROVIDER` (`GEMINI_API_KEY`, `GROQ_API_KEY`, atau `ANTHROPIC_API_KEY`),
semua fitur tetap jalan lewat cadangan berbasis aturan (sama dengan mode demo prototipe), dan responsnya
ditandai `engine: "fallback"`. Cek penyedia yang aktif di `GET /health`.

### Akun demo (token dev)

Di produksi aplikasi login ke Supabase Auth dan mengirim access token-nya. Untuk dev, `seed_demo` membuat
organisasi fiktif **PT Sinar Distribusi** (48 karyawan contoh di 5 departemen, sama seperti `DEPTS` prototipe)
dan undangan untuk tiga akun:

```bash
uv run python -m scripts.dev_token --email rina@sinar-distribusi.test --name "Rina Lestari"    # pekerja
uv run python -m scripts.dev_token --email ratna@sinar-distribusi.test --name "Ratna Wijaya"   # atasan tim (7 anggota)
uv run python -m scripts.dev_token --email hr@sinar-distribusi.test --name "Hana Pratiwi"      # HR
uv run python -m scripts.dev_token --email tim@levelin.test --role reviewer                     # peninjau
```

## Data

`data/hearz/` berisi data asli tim dari prototipe:

- **Jabatan dan tugas**: 10 jabatan admin/klerikal + 9 jabatan tujuan (arah *naik*/*pindah*), 87 tugas. Skor
  tugas dari ILO-NASK Global AI Exposure Index 2025 (Gmyrek dkk., ILO Working Paper 140), lisensi CC BY 4.0.
  Teks tugas diterjemahkan dan diringkas oleh Tim Hearz; adaptasi ini tidak disiapkan, ditinjau, atau disahkan
  oleh ILO, dan tanggung jawabnya ada pada Tim Hearz.
- **Skill** (10), **jenis tugas** (6, penghubung tugas ke skill dan misi), **misi** M1-M3 lengkap dengan kunci
  jawaban, **kelas** (12, harga dicek 21 Oktober 2026 dan bisa berubah).
- Perusahaan, atasan, lowongan, dan data tim fiktif.

Untuk data penuh, skrip `scripts/ingest_*.py` mengimpor KBJI 2026 (BPS), indeks ILO, ESCO, katalog kelas, dan
bank misi. Format kolom ada di docstring tiap skrip; jalankan `scripts.embed_reference` sesudahnya.

## Peran

| Peran | Akses |
|---|---|
| `worker` | Aplikasi pekerja: audit, rencana, kelas, misi, Paspor, lowongan |
| `manager` | Atasan tim: undang anggota tim sendiri (`/v1/team/*`), lihat agregat tim (k-anonim) dan anggota yang berbagi Paspor |
| `hr_admin` | HR: agregat organisasi, karyawan & peran, undangan, misi kustom, lowongan internal, laporan |
| `reviewer` | Tim Levelin: antrean penilaian dan bank misi |

Peran dibaca dari `app_metadata.role` di JWT bila ada, atau diberikan lewat undangan / `PATCH /v1/org/members/{id}`.
Undangan organisasi dan undangan tim diterima otomatis saat pengguna membuka `GET /v1/me`. Pekerja bisa keluar
dari tim lewat `DELETE /v1/me/teams/{manager_id}`.

## Aturan (sama dengan prototipe)

Rumus ada di [app/services/scoring.py](app/services/scoring.py):

- Kelompok tugas: skor ILO `>= 0,70` **auto** (Diotomasi), `>= 0,40` **assist** (Dibantu AI), sisanya **human**
  (Tetap Manusia). Tugas tanpa padanan ILO = **none**, kecuali pengguna memilih kelompoknya sendiri.
- Jam dihemat = jam auto × 0,6 + jam assist × 0,3 (faktor asumsi).
- Skor Kesiapan = porsi jam terpapar (auto + assist) yang sudah siap: penuh bila misi untuk jenis tugas itu sudah
  lulus, setengah bila tugas itu sudah dikerjakan dengan bantuan AI. Dihitung ulang setiap misi lulus/batal dan
  disimpan per bulan untuk grafik tren.
- Rencana: arah **perkuat** (skill AI + skill dua jenis tugas paling terpapar), **naik**, atau **pindah** (skill
  jabatan tujuan + AI). Misi direkomendasikan menurut jenis tugas paling terpapar; kelas diurutkan menurut
  jumlah celah skill yang ditutup lalu harga (label Mitra tidak memengaruhi urutan).
- Misi lulus = semua cek objektif benar **dan** rata-rata rubrik ≥ 3. Tanpa LLM: semua cek benar dan jawaban
  tertulis minimal 25 kata; hasil seperti ini selalu masuk antrean peninjau.
- Lowongan: tiap skill yang diminta bernilai 1 bila terbukti lewat misi, 0,5 bila skill dasar (skill bawaan
  jabatan + skill jenis tugas di Peta Tugas). Lowongan internal tampil lebih dulu; sumber tak terverifikasi
  diberi peringatan.

## Privasi dan keamanan

**Kunci jawaban tidak keluar server.** `GET /v1/missions/{id}` tidak memuat `answer_key`; cek objektif jalan di
[app/services/grading.py](app/services/grading.py). Atasan AI tidak menerima kunci jawaban, dan kalimat balasan
yang memuat nilai jawaban (`20.980.000`, `20,98 juta`, `8 persen`) diganti sebelum dikirim. Jawaban pilihan
(mis. nama cabang) tidak bisa disaring karena semua opsinya terlihat, jadi hanya dicegah oleh prompt.

**HR dan atasan hanya melihat agregat.** Angka organisasi, tim, departemen, dan bulan disembunyikan bila orang
yang sudah audit kurang dari `K_ANONYMITY_MIN` (default 5). Bila sisa yang disembunyikan masih di bawah ambang,
departemen terkecil ikut disembunyikan supaya angkanya tidak bisa dihitung dari selisih total
(`suppressed_departments`). Kandidat hanya pekerja yang menyalakan berbagi Paspor, dan isinya skill terbukti,
bukan audit.

**Endpoint AI punya cadangan.** Keluaran JSON LLM memakai structured output lalu divalidasi Pydantic; gagal,
ditolak, atau error API (termasuk kuota habis) berarti cadangan berbasis aturan dipakai. Rate limit per
pengguna dibalas `429` dengan `Retry-After`. Prompt melarang topik PHK; keluaran juga difilter per kalimat,
termasuk saat streaming dan pada umpan balik rubrik.

## Streaming (SSE)

`POST /v1/audits/{id}/explain`, `POST /v1/plans/{id}/narrative`, dan `POST /v1/attempts/{id}/messages`:

```
event: meta    data: {"engine": "llm"}                     sebelum teks pertama
event: delta   data: {"text": "..."}                       potongan teks, gabungkan berurutan
event: reset   data: {"reason": "llm_error"}               buang teks yang sudah tampil; cadangan menyusul
event: done    data: {"engine": "llm", "message_id": "...", "flagged": false}
```

Rate limit dan kepemilikan dicek sebelum stream dibuka. Karena memakai POST, gunakan klien yang mendukung POST
(Flutter: `http` dengan stream response; web: `fetch` + `ReadableStream`).

## Paginasi dan cache

Endpoint daftar menerima `?page=&limit=` (maks 100) dan membalas `{"data": [...], "meta": {page, limit, total,
total_pages, has_next}}`. Data referensi (`/v1/occupations*`, `/v1/skills`) mengirim `Cache-Control: public, max-age=3600`.

## Fase 5: AI Skill Coach dan generator misi

Semua endpoint berikut memerlukan JWT. Dua endpoint coach tersedia untuk pekerja; generator misi hanya untuk
`reviewer`. Endpoint coach dan evaluasi mengembalikan `engine` (`llm` atau `fallback`) dan tidak menggunakan
kuota misi.

### `POST /v1/skills/{skill_id}/coach`

Membuat panduan praktik kerja sekitar 3 menit untuk skill, template prompt AI, dan latihan mikro. Tidak memerlukan
body request.

```http
POST /v1/skills/12/coach
Authorization: Bearer <token>
```

```json
{
  "skill": {"id": 12, "label": "Analisis data"},
  "category": "digital",
  "engine": "llm",
  "overview": "Gunakan data untuk menemukan pola dan menyusun rekomendasi.",
  "key_steps": ["Tentukan pertanyaan", "Periksa kualitas data", "Sampaikan temuan"],
  "prompt_templates": [{"title": "Cari pola", "when_to_use": "Saat meninjau laporan", "prompt": "Ringkas pola utama dari data berikut: ..."}],
  "micro_drill": {"scenario": "...", "question": "...", "evaluation_criteria": ["Akurasi"], "hint": "Periksa angka pembanding."},
  "related_missions": []
}
```

### `POST /v1/skills/{skill_id}/drill-check`

Menilai jawaban latihan mikro secara instan. Kirim skenario, pertanyaan, dan jawaban; hasil memberi skor, umpan
balik, kekuatan, hal yang perlu ditingkatkan, dan contoh jawaban. Evaluasi ini tidak memotong kuota misi.

```http
POST /v1/skills/12/drill-check
Authorization: Bearer <token>
Content-Type: application/json

{"scenario":"Laporan penjualan naik 8% bulan ini.","question":"Apa yang perlu diperiksa sebelum menyimpulkan?","answer":"Saya akan membandingkan periode sebelumnya dan memeriksa sumber datanya."}
```

```json
{
  "skill_id": 12,
  "score": 85,
  "ready_for_mission": true,
  "feedback": "Jawaban sudah menyebut pembanding dan validasi data.",
  "strengths": ["Memeriksa sumber data"],
  "improvements": ["Sebutkan rentang periode pembanding"],
  "model_answer": "Bandingkan dengan periode sebelumnya dan validasi sumber serta cakupan data.",
  "engine": "llm"
}
```

### `POST /v1/admin/missions/generate`

Membuat **draf** simulasi kerja dari judul dan SOP perusahaan atau deskripsi topik. Endpoint ini khusus untuk
`reviewer`; respons berisi skenario, data simulasi, rubrik, dan kunci jawaban untuk ditinjau sebelum misi dimasukkan
ke bank misi.

```http
POST /v1/admin/missions/generate
Authorization: Bearer <token-reviewer>
Content-Type: application/json

{"title":"Menangani retur barang","description":"SOP: verifikasi bukti pembelian, kondisi barang, dan batas waktu retur.","target_role":"Staf layanan pelanggan","kind":"simulasi","difficulty":2}
```

```json
{
  "slug": "menangani-retur-barang",
  "title": "Menangani retur barang",
  "summary": "Verifikasi permintaan retur berdasarkan SOP.",
  "brief": "...",
  "simulation_data": {"permintaan_retur": "..."},
  "answer_fields": [{"name": "keputusan", "label": "Keputusan retur"}],
  "rubric": [{"criterion": "Mengikuti SOP", "weight": 1}],
  "answer_key": [{"field": "keputusan", "expected": "..."}],
  "skill_ids": [12],
  "kind": "simulasi",
  "difficulty": 2,
  "est_minutes": 10,
  "engine": "llm"
}
```

## Tugas terjadwal

`uv run python -m scripts.send_reminders` (harian): pengingat cek ulang Peta Tugas setiap `REMINDER_DAYS` hari
dan pengingat misi untuk rencana yang mandek 7 hari. Notifikasi disimpan in-app dan dikirim lewat FCM bila
`FCM_SERVICE_ACCOUNT_FILE` diisi.

## Tes

```bash
uv run pytest                                                         # SQLite in-memory
TEST_DATABASE_URL=postgresql+asyncpg://.../levelin_test uv run pytest # Postgres + pgvector
```

Tes mencakup alur Rina dari prototipe (L1-L10), rumus skor dan cek misi M1-M3, jalur LLM dengan LLM palsu
(validasi skema, filter di stream dan rubrik, reset saat stream putus), bentuk request ke SDK Google, Groq, dan
Anthropic, dashboard HR dan tim k-anonim, peran atasan, peninjau, dan webhook pembayaran.

## Struktur

```
app/
  api/v1/        router: me, reference, audits, plans, courses, missions, attempts, passport, jobs,
                 org (HR), team (atasan), notifications, billing, admin
  services/      logika: skor, audit, LLM, embedding, guardrail, penilaian, rencana, HR/tim, pembayaran
  models/        tabel SQLAlchemy
  schemas/       kontrak request/response
  core/          konfigurasi, auth JWT, format error, paginasi, rate limit
alembic/         migrasi database
scripts/         seed, impor data, embedding, pengingat, token dev
data/hearz/      data tim dari prototipe (jabatan, tugas ILO-NASK, skill, misi, kelas, lowongan contoh)
```

## Deploy

`Dockerfile` menjalankan migrasi lalu uvicorn di `$PORT`. Set variabel `.env` di platform hosting.
Untuk lebih dari satu replika, jalankan `alembic upgrade head` sebagai langkah release terpisah.
Isi `SUPABASE_SERVICE_ROLE_KEY` di produksi: tanpa itu `DELETE /v1/me` hanya menghapus data di backend,
bukan akun Supabase Auth.
