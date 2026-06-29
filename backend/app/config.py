from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_secret_key: str

    database_url: str

    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440

    bootstrap_admin_login: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_email: str = ""

    login_rate_limit: str = "5/minute"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    smtp_use_tls: bool = True

    billing_domain: str = "example-learning.ru"

    telegram_bot_token: str = ""
    admin_telegram_ids: str = ""
    telegram_login_bot_username: str = ""

    yukassa_shop_id: str = ""
    yukassa_secret_key: str = ""
    yukassa_webhook_secret: str = ""
    yukassa_stub_mode: bool = True
    yukassa_api_url: str = "https://api.yookassa.ru/v3"
    payment_description: str = "Благодарность за образовательные материалы"

    site_base_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    cookie_name: str = "vxn_access_token"

    marzban_base_url: str = "http://10.10.0.1:8000"
    marzban_admin_user: str = "admin"
    marzban_admin_password: str = ""
    marzban_token_cache_minutes: int = 55
    marzban_request_timeout_seconds: float = 30.0

    marzban_job_poll_interval_seconds: int = 60
    marzban_job_max_attempts: int = 10
    marzban_job_retry_delay_seconds: int = 120

    reminder_cron_hour: int = 10
    reminder_timezone: str = "Europe/Moscow"


@lru_cache
def get_settings() -> Settings:
    return Settings()
