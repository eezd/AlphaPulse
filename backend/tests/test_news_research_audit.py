"""Fixed-version and evidence behavior, isolated from production I/O."""

import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
import pytest


@pytest.mark.parametrize(
    "event_id,state", [(2, "done"), (1, "failed"), (1, "superseded")]
)
def test_fixed_version_rejects_wrong_event_and_unsuccessful_versions(
    trading, event_id, state
):
    from fastapi import HTTPException
    from alphapulse.news_research.api import event_version

    session = N(get=AsyncMock(return_value=N(event_id=event_id, state=state)))
    with pytest.raises(HTTPException) as error:
        asyncio.run(event_version(1, 9, session))
    assert error.value.status_code == 404


def test_acceptance_budget_is_shared_and_counts_failed_streams(trading, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "acceptance_budget",
        Path(__file__).resolve().parents[2] / "scripts/accept_news_research.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    counter = tmp_path / "requests"
    first = module.Budget(counter, limit=2)
    second = module.Budget(counter, limit=2)
    first.take()
    second.take()
    with pytest.raises(module.BudgetExceeded):
        first.take()
    assert counter.read_text() == "2"


def test_budget_counts_transport_failures_before_retry(trading, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "failed_stream_budget",
        Path(__file__).resolve().parents[2] / "scripts/accept_news_research.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    budget = module.Budget(tmp_path / "counter", limit=2)
    sent = []

    async def failing():
        sent.append(True)
        raise ConnectionError("isolated failure")
        yield

    async def attempt():
        async for _ in budget.wrap(failing)():
            pass

    async def run():
        return await asyncio.gather(
            *(attempt() for _ in range(3)), return_exceptions=True
        )

    errors = asyncio.run(run())
    assert len(sent) == 2
    assert sum(isinstance(e, module.BudgetExceeded) for e in errors) == 1


def test_supplemental_citation_must_use_returned_article_id(trading):
    from alphapulse.news_research.pipeline import validate_evidence, Evidence

    sources = {-1: {"news_id": 1004, "content": "Original correction"}}
    validate_evidence([Evidence(article_id=-1, quote="Original correction")], sources)
    with pytest.raises(ValueError):
        validate_evidence(
            [Evidence(article_id=1004, quote="Original correction")], sources
        )
