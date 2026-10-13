from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 容器内由 compose 的 env_file 注入；只有一套配置，见仓库根目录 .env
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str
    admin_username: str
    admin_password_hash: str = ""
    jwt_expire_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()
