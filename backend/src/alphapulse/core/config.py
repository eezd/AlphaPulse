from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 由服务环境注入；本机私有配置位于 runtime/config/app.env。
    model_config = SettingsConfigDict(extra="ignore")

    congress_api_key: str = ""
    crawler_contact_email: str = ""

    database_url: str
    redis_url: str
    jwt_secret: str
    admin_username: str
    admin_password_hash: str = ""
    login_captcha_enabled: bool = False
    jwt_expire_days: int = 7
    # Fernet 密钥，加密存库的第三方 API key
    secret_encryption_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
