"""Replay orchestration must drain or cancel every synthetic task."""
import asyncio
import importlib.util
from pathlib import Path
import pytest


def load_replay():
    path=Path(__file__).resolve().parents[2]/'scripts/replay_news_sample.py'
    spec=importlib.util.spec_from_file_location('sample_replay_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_refills_before_slow_task_finishes(trading):
    replay=load_replay()
    async def run():
        third_started=asyncio.Event()
        order=[]
        async def action(identifier):
            if identifier == 1: await third_started.wait()
            if identifier == 3: third_started.set()
            order.append(identifier)
        await asyncio.wait_for(replay.bounded([1,2,3],action,2),1)
        assert order.index(3)<order.index(1)
    asyncio.run(run())


def test_replay_failure_cancels_and_drains_other_tasks(trading):
    replay=load_replay()
    async def run():
        started=asyncio.Event()
        drained=[]
        async def action(identifier):
            if identifier == 1:
                await started.wait()
                raise ValueError('synthetic failure')
            try:
                started.set()
                await asyncio.Event().wait()
            finally: drained.append(identifier)
        with pytest.raises(ValueError,match='synthetic failure'):
            await replay.bounded([1,2],action,2)
        assert drained==[2]
    asyncio.run(run())
