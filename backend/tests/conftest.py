import pytest


@pytest.fixture
def app_env(monkeypatch):
    # 测试只替换 I/O 边界，不读取项目凭证，也不连接线上数据库或 Redis。
    for key, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1/test",
        "REDIS_URL": "redis://127.0.0.1:1/0", "JWT_SECRET": "test-only",
        "ADMIN_USERNAME": "test", "SECRET_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    }.items():
        monkeypatch.setenv(key, value)
    from unittest.mock import AsyncMock
    from alphapulse.ai import llm, recovery
    from alphapulse.ai import progress
    originals = {name: getattr(recovery, name) for name in (
        'availability', 'mark_waiting', 'clear_waiting', 'waiting_count')}
    originals['retry_scope'] = llm.retry_scope
    originals['sync_once'] = progress.sync_once
    monkeypatch.setattr(progress, 'sync_once', AsyncMock())
    monkeypatch.setattr(llm, 'retry_scope', lambda _: 'synthetic-scope')
    monkeypatch.setattr(recovery, 'availability', AsyncMock(return_value={
        'state': 'normal', 'retry_at': None, 'level': 0}))
    monkeypatch.setattr(recovery, 'mark_waiting', AsyncMock())
    monkeypatch.setattr(recovery, 'clear_waiting', AsyncMock())
    monkeypatch.setattr(recovery, 'waiting_count', AsyncMock(return_value=0))
    return originals


@pytest.fixture
def briefing_worker_env(app_env, monkeypatch):
    """只隔离登记与外部发送；调度、撤销、信号和排空保持真实。"""
    from unittest.mock import AsyncMock
    from alphapulse.briefings import worker

    async def settings(session, **kwargs):
        return await session.get(None, 1)

    async def sender(stopping):
        await stopping.wait()

    monkeypatch.setattr(worker.config, 'settings_row', settings)
    monkeypatch.setattr(worker.service, 'enroll', AsyncMock())
    monkeypatch.setattr(worker.reports, 'prepare_due', AsyncMock())
    monkeypatch.setattr(worker.notifications, 'recover', AsyncMock())
    monkeypatch.setattr(worker.notifications, 'sender', sender)
