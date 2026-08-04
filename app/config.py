from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Intellectual Game Bot"
    database_url: str = "sqlite:///./game_v9.db"
    telegram_bot_token: str = ""
    organizer_telegram_id: str = "707309709"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    secret_key: str = "replace-this-in-production"
    web_base_url: str = "http://127.0.0.1:8000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
