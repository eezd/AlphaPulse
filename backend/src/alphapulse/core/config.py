from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 容器内由 compose 的 env_file 注入；只有一套配置，见仓库根目录 .env
    model_config = SettingsConfigDict(extra="ignore")

    searxng_url: str = "http://searxng:8080"
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
