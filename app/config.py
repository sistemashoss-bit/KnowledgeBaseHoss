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

    # Wasabi S3-compatible
    wasabi_access_key: str
    wasabi_secret_key: str
    wasabi_bucket_name: str
    wasabi_avatar_bucket_name: str
    wasabi_region: str = "us-east-1"
    wasabi_endpoint_url: str = "https://s3.wasabisys.com"

    # Auth
    jwt_secret: str
    jwt_expire_minutes: int = 480
    csrf_secret: str

    # OpenRouter
    openrouter_api_key: str
    openrouter_model: str = "anthropic/claude-3.5-haiku"


    @property
    def sqlalchemy_url(self) -> str:
        return self.db_url.replace("postgres://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
