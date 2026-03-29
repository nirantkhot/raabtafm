from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://raabta:raabta@localhost:5432/raabta"
    redis_url: str = "redis://localhost:6379"

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    seed_playlists: str = ""

    rate_limit_vibe_per_minute: int = 10
    rate_limit_ws_per_hour: int = 20
    rate_limit_sessions_per_hour: int = 5
    max_session_members: int = 10

    csv_path: str = "/app/data/spotify_features.csv"

    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
