from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False

    RATE_LIMITING_ENABLE: bool = False
    RATE_LIMITING_FREQUENCY: str = "2/3seconds"

    CACHE_ENABLE: bool = True
    CACHE_DIR: str = ".cache/http"
    CACHE_TTL_SECONDS: int = 3600
    CACHE_SIZE_LIMIT_MB: int = 500

    OUTBOUND_MIN_INTERVAL_MS: int = 500
    OUTBOUND_MAX_RETRIES: int = 4


settings = Settings()
