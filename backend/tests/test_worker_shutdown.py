"""Service shutdown drains the existing AI task without admitting its successor."""
import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def test_research_sigterm_drains_inflight_task(app_env):
    from alphapulse.news_research import worker

    async def scenario():
        handlers = {}
        finished = []
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.scalar.return_value = True
        engine = SimpleNamespace(connect=lambda: connection)
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.get.return_value = SimpleNamespace(enabled=True, config={'concurrency': 1}, control_revision=1)
        async def process(*args):
            handlers[signal.SIGTERM]()
            await asyncio.sleep(.01)
            finished.append(True)
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'add_signal_handler', side_effect=lambda sig, callback: handlers.update({sig: callback})), \
             patch.object(worker, 'engine', engine), patch.object(worker, 'SessionLocal', return_value=session), \
             patch.object(worker, 'recover_interrupted', new=AsyncMock()), \
             patch.object(worker.scheduling, 'actions', new=AsyncMock(return_value=[('package', 1)])) as actions, \
             patch.object(worker, 'process_package', side_effect=process):
            await asyncio.wait_for(worker.run(), 2)
        assert finished == [True]
        assert actions.await_count == 1
    asyncio.run(scenario())
