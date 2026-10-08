from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    db_url: str

    # OpenSearch
    opensearch_url: str

    # Valkey / Redis-compatible cache
    valkey_url: str = ""

    # Chat retention
    message_retention_days: int = 30
    cleanup_hour_utc: int = 3  # daily purge runs at this UTC hour

    # Hora local del negocio (México no tiene horario de verano desde 2022 → UTC-6 fijo).
    local_utc_offset_hours: int = -6
    recurring_hour_local: int = 6  # las tareas recurrentes se generan a esta hora local

    # Audit / search log retention
    audit_retention_days: int = 60

    # Tareas: días en "Listo" antes de archivarse automáticamente del Kanban
    task_archive_after_days: int = 7

    # Wasabi S3-compatible — solo las credenciales son secretas (van en .env).
    # Los nombres de bucket, región y endpoint son estables y viven aquí.
    wasabi_access_key: str
    wasabi_secret_key: str
    wasabi_bucket_name: str = "knowledgehoss"
    wasabi_avatar_bucket_name: str = "hossavatars"
    wasabi_evidence_bucket_name: str = "hossevidences"
    wasabi_chats_bucket_name: str = "hosschats"
    wasabi_region: str = "us-east-1"
    wasabi_endpoint_url: str = "https://s3.wasabisys.com"

    # Auth
    jwt_secret: str
    jwt_expire_minutes: int = 480
    csrf_secret: str

    # SSO con hoss-api (proveedor de identidad del staff).
    # Si queda vacío, knowledge usa solo autenticación local.
    hoss_api_url: str = ""

    # OpenRouter (LLM for Q&A)
    openrouter_api_key: str
    openrouter_model: str = "qwen/qwen3.7-flash"

    # Widget de reportes: Jev (TypeSafe) vía OpenRouter, con la misma API key,
    # clasifica departamento y severidad. Versión fijada (no el alias -latest)
    # para que los umbrales no cambien solos con un release nuevo.
    jev_model: str = "typesafe/jev-1.13"
    jev_timeout_seconds: float = 8.0
    # Departamento al que cae un reporte cuando Jev no está seguro o falla.
    report_triage_department: str = "Sistemas"
    # Orígenes (coma-separados) que pueden llamar /api/issue-reports desde el
    # widget, p. ej. "https://app.hoss.com.mx,https://meta.hoss.com.mx".
    report_widget_origins: str = ""

    # Voyage AI (embeddings + rerank)
    voyage_api_key: str
    voyage_embedding_model: str = "voyage-4-large"
    voyage_embedding_dim: int = 2048
    voyage_rerank_model: str = "rerank-2.5"

    # Web Push (notificaciones fuera de la pestaña, vía service worker).
    # Vacíos = push deshabilitado (silenciosamente): el resto de notificaciones
    # (dropdown + SSE) sigue funcionando igual. El correo admin (claim VAPID
    # "sub") no es secreto y no depende del entorno — está fijo en app/push.py.
    vapid_public_key: str = ""
    vapid_private_key: str = ""


    @property
    def report_widget_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.report_widget_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        return self.db_url.replace("postgres://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
