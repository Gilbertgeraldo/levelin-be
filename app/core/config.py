from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_name: str = "Levelin API"
    app_version: str = "0.2.0"
    # Dipisah koma, mis. "https://hr.levelin.id,http://localhost:3000"
    cors_origins: str = "*"
    # Basis URL front-end untuk tautan Paspor publik: {base}/{slug}
    passport_public_base_url: str = "https://levelin.id/p"

    # Database. Supabase memberi "postgresql://..."; URL dinormalisasi ke driver asyncpg.
    database_url: str = "sqlite+aiosqlite:///./levelin.db"
    # True bila memakai pooler Supabase mode transaksi (port 6543 / pgbouncer).
    db_pgbouncer: bool = False
    db_echo: bool = False

    # Auth (Supabase). HS256 memakai JWT secret; RS256/ES256 memakai JWKS dari supabase_url.
    supabase_url: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_service_role_key: str | None = None
    # Tujuan tautan di email undangan: web dashboard (HR & atasan) dan deep link aplikasi (pekerja)
    web_url: str = "http://localhost:3000"
    app_auth_redirect_url: str | None = None
    # Login akun demo tanpa kata sandi (POST /v1/dev/demo-login). Jangan nyalakan di produksi.
    dev_login_enabled: bool = False

    # LLM. "main" untuk ekstraksi, atasan AI, rubrik; "light" untuk penjelasan & narasi.
    llm_provider: Literal["gemini", "groq", "anthropic"] = "gemini"
    llm_timeout_seconds: float = 60.0
    gemini_api_key: str | None = None
    gemini_model_main: str = "gemini-3.8-flash"
    gemini_model_light: str = "gemini-3.5-flash-lite"
    groq_api_key: str | None = None
    groq_model_main: str = "openai/gpt-oss-120b"
    groq_model_light: str = "llama-3.1-8b-instant"
    anthropic_api_key: str | None = None
    llm_model_main: str = "claude-opus-5-5"
    llm_model_light: str = "claude-haiku-5-5"
    llm_server_fallback: bool = True

    # Embedding
    embedding_provider: str = "voyage"  # voyage | hashing
    voyage_api_key: str | None = None
    embedding_model: str = "voyage-3.5"
    embedding_dim: int = 1024
    # Kemiripan kosinus minimum agar tugas cerita dianggap cocok dengan template ILO.
    match_min_similarity: float = 0.35

    # Rate limit AI per pengguna
    rate_limit_audit_per_day: int = 10
    rate_limit_explain_per_hour: int = 20
    rate_limit_narrative_per_hour: int = 20
    rate_limit_chat_per_hour: int = 60
    rate_limit_grade_per_day: int = 20

    # Aturan produk
    k_anonymity_min: int = 5
    review_sample_rate: float = 0.10
    free_mission_quota: int = 3
    org_member_mission_quota: int = 20
    reminder_days: int = 90

    # Pembayaran (Midtrans Snap)
    midtrans_server_key: str | None = None
    midtrans_is_production: bool = False
    plan_plus_price: int = 49_000
    plan_plus_quota: int = 30
    plan_plus_days: int = 30
    plan_jalur_price: int = 149_000
    plan_jalur_quota: int = 100
    plan_jalur_days: int = 90

    # Penyimpanan berkas (dokumen misi kustom)
    storage_backend: str = "local"  # local | supabase
    upload_dir: str = "./uploads"
    supabase_storage_bucket: str = "custom-missions"
    max_upload_mb: int = 10

    # Push notification (FCM HTTP v1)
    fcm_service_account_file: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix) :]
        # asyncpg tidak mengenal sslmode; ubah ke ssl
        if url.startswith("postgresql+asyncpg://"):
            url = url.replace("sslmode=", "ssl=")
        return url

    @property
    def is_postgres(self) -> bool:
        return self.async_database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
