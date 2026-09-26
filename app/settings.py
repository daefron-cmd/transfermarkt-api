from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False
    # Proxy addresses whose X-Forwarded-For header uvicorn trusts ("*" = any). The uvicorn CLI (Docker) reads the
    # FORWARDED_ALLOW_IPS environment variable itself; this setting passes it on for `python -m app.main`.
    FORWARDED_ALLOW_IPS: str = "127.0.0.1"
    LOG_LEVEL: str = "INFO"
    # Comma-separated origins allowed to call the API from a browser; empty disables CORS.
    CORS_ORIGINS: str = ""

    RATE_LIMITING_ENABLE: bool = False
    RATE_LIMITING_FREQUENCY: str = "2/3seconds"

    CACHE_ENABLE: bool = True
    CACHE_DIR: str = ".cache/http"
    CACHE_TTL_SECONDS: int = 3600
    CACHE_SIZE_LIMIT_MB: int = 500

    OUTBOUND_MIN_INTERVAL_MS: int = 500
    OUTBOUND_MAX_RETRIES: int = 4
    OUTBOUND_MAX_CONCURRENCY: int = 4
    OUTBOUND_TIMEOUT_S: float = 30


settings = Settings()
