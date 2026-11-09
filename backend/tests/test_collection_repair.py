"""Repair initialization never renews an already initialized retry budget."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_preview_and_repeated_execution_preserve_retry_evidence(app_env, monkeypatch):
    from alphapulse.crawler import repair_collection as module
    original = {'document_retries': {'https://example.invalid/doc': {
        'document': {'url': 'https://example.invalid/doc'}, 'attempts': 3,
        'next_at': 1, 'error': 'HTTP 503'}}}
    source = SimpleNamespace(code='doj', history_cursor=copy.deepcopy(original))
    session = AsyncMock()
    session.scalars.return_value = [source]
    session.__aenter__.return_value = session
    monkeypatch.setattr(module, 'SessionLocal', lambda: session)
    monkeypatch.setattr(module, 'engine', SimpleNamespace(dispose=AsyncMock()))
    async def check():
        await module.run()
        assert source.history_cursor == original
        session.commit.assert_not_awaited()
        await module.run(True)
        entry = source.history_cursor['document_retries']['https://example.invalid/doc']
        assert entry['attempts'] == 0 and entry['repair_previous']['attempts'] == 3
        entry['attempts'] = 2
        result = await module.run(True)
        assert result['sources'][0]['already_initialized']
        assert entry['attempts'] == 2
    asyncio.run(check())
