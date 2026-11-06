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
    from unittest.mock import AsyncMock
    from alphapulse.ai import llm, recovery
    class HealthyGate:
        async def call(self, operation, request_id='', epoch=-1):
            return {'epoch': 0, 'level': 0, 'retry_at': 0, 'probe': False, 'admitted': True, 'now': 0}
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: HealthyGate())
    monkeypatch.setattr(recovery, 'mark_waiting', AsyncMock())
    monkeypatch.setattr(recovery, 'clear_waiting', AsyncMock())
    monkeypatch.setattr(recovery, 'waiting_count', AsyncMock(return_value=0))
    return executor
