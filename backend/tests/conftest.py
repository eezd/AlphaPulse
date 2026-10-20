import pytest


@pytest.fixture
def trading(monkeypatch):
    # 测试只替换 I/O 边界，不读取项目凭证，也不连接线上数据库或 Redis。
    for key, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1/test",
        "REDIS_URL": "redis://127.0.0.1:1/0", "JWT_SECRET": "test-only",
        "ADMIN_USERNAME": "test", "SECRET_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    }.items():
        monkeypatch.setenv(key, value)
    from alphapulse.trading import executor
    return executor
