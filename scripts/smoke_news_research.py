"""Verify two-stage behavior in a disposable CI database using mock model I/O."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlparse
from sqlalchemy import select, func, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research import service
from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version
from alphapulse.news_research.contracts import IndexResult, Assignment, ResearchResult


async def main():
    url = urlparse(os.environ["DATABASE_URL"])
    assert url.username == "ci" and url.password == "ci-only" and url.path == "/ci"
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        news = NewsItem(
            source_code="wscn_news",
            external_id="ci-research-" + str(now.timestamp()),
            title="Original incident and response",
            summary="",
            content_text="Original incident and response",
            content_hash="c" * 64,
            url="https://example.invalid/ci",
            published_at=now,
        )
        s.add(news)
        await s.commit()
        news_id = news.id
        await service.enroll(s, [news_id])
        await service.enroll(s, [news_id])
        package = await s.scalar(select(Package).where(Package.news_id == news_id))
        identifier = package.id
        assert (
            await s.scalar(
                select(func.count())
                .select_from(Package)
                .where(Package.news_id == news_id)
            )
            == 1
        )
    calls = []
    model_inputs = {}

    async def config(*args):
        return SimpleNamespace(context_window=1000000, max_tokens=1000), "test", {}

    async def complete(provider, skill, payload, schema):
        calls.append(schema.__name__)
        model_inputs[schema.__name__] = deepcopy(payload)
        if schema is IndexResult:
            return IndexResult(
                keywords=[
                    {
                        "canonical": "incident",
                        "aliases": ["incident"],
                        "quote": "Original incident and response",
                    }
                ]
            ), {}
        return Assignment(
            targets=[
                {
                    "event_id": None,
                    "new_key": str(i),
                    "title": "Incident " + str(i),
                    "reason": "A related part",
                    "quote": "Original incident and response",
                    "keyword_canonicals": ["incident"],
                    "change_kind": "new_fact",
                    "urgent": False,
                }
                for i in (1, 2)
            ]
        ), {}

    with (
        patch.object(service, "model_config", config),
        patch.object(service, "complete", complete),
    ):
        await service.process_package(identifier, {})
        await service.process_package(identifier, {})
    assert calls == ["IndexResult", "Assignment"]
    async with SessionLocal() as s:
        attempts = list(
            await s.scalars(select(Attempt).where(Attempt.package_id == identifier))
        )
        assert {a.stage: a.input for a in attempts} == {
            "index": model_inputs["IndexResult"],
            "assign": model_inputs["Assignment"],
        }, "Persist the exact model input, including assignment keywords and candidates"
        for event in await s.scalars(select(Event)):
            assert event.due_at - event.dirty_since == timedelta(minutes=5)
        assert (
            await s.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.package_id == identifier)
            )
            == 2
        )
        assert (
            await s.get(NewsItem, news_id)
        ).content_text == "Original incident and response"
        package = await s.get(Package, identifier)
        package.state = "assign_pending"
        await s.commit()
    with (
        patch.object(service, "model_config", config),
        patch.object(
            service, "complete", side_effect=ValueError("classification failure")
        ),
    ):
        await service.process_package(identifier, {})
    async with SessionLocal() as s:
        package = await s.get(Package, identifier)
        assert package.state == "assign_failed" and package.keywords
        failed = await s.scalar(
            select(Attempt)
            .where(Attempt.package_id == identifier, Attempt.stage == "assign")
            .order_by(Attempt.id.desc())
        )
        assert failed.input["keywords"] == package.keywords
        assert len(failed.input["events"]) == 2
        assert (
            await s.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.package_id == identifier, Attempt.stage == "index")
            )
            == 1
        )
        # Evidence validation failure is retained as a failed version, never a success.
        event = await s.scalar(
            select(Event)
            .join(Membership)
            .where(Membership.package_id == identifier)
            .limit(1)
        )
        package.state = "done"
        event.state = "pending"
        event.due_at = now
        await s.commit()
        event_id = event.id

    async def run(self, *args):
        self.records.append(
            {
                "tool": "read_news",
                "arguments": {"id": 2147483647},
                "result": {
                    "article_id": -1,
                    "news_id": 2147483647,
                    "content": "Previously verified background",
                },
            }
        )
        result = ResearchResult(
            id=event_id,
            title="Incident",
            summary="Summary",
            category="安全",
            entities=[],
            risk_stars=1,
            importance_stars=2,
            direction="unknown",
            verification="insufficient",
            confidence="low",
            reasoning="Unknown",
            uncertainty="Unknown",
            facts="Original",
            timeline="Unknown",
            asset_impacts="Unknown",
            opinions="Unknown",
            counter_evidence="Unknown",
            watchpoints="Unknown",
            claims=[
                {
                    "statement": "Original",
                    "verdict": "insufficient",
                    "evidence_refs": [],
                }
            ],
            evidence_refs=[{"article_id": news_id, "quote": "Invented"}],
            timeline_points=[
                {
                    "package_ids": [identifier],
                    "occurred_at": None,
                    "time_precision": "unknown",
                    "time_text": "",
                    "reported_at": now,
                    "description": "Original",
                    "verification": "partial",
                    "evidence_refs": [
                        {
                            "article_id": news_id,
                            "quote": "Original incident and response",
                        }
                    ],
                }
            ],
            references=[],
            excluded_article_ids=[],
        )
        return result, {}

    with (
        patch.object(service, "model_config", config),
        patch.object(service.Research, "run", run),
    ):
        await service.process_event(event_id, {}, force=True)
    async with SessionLocal() as s:
        event = await s.get(Event, event_id)
        assert event.state == "failed" and event.result is None
        version = await s.scalar(select(Version).where(Version.event_id == event_id))
        assert (
            version.state == "failed"
            and version.output["evidence_refs"][0]["quote"] == "Invented"
        )
        assert (
            await s.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'news_research_%'"
                )
            )
            == 8
        )
    # Exercise actual routes and published-Skill resolution against isolated storage.
    import httpx
    from fastapi import FastAPI
    from alphapulse.news_research import api
    from alphapulse.models import AiProvider, SkillNode, SkillVersion
    from alphapulse.core.crypto import encrypt
    from alphapulse.news_research import model as model_service

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://ci"
    ) as client:
        assert (await client.get("/api/risk/events?page=0")).status_code == 422
        assert (await client.get("/api/risk/packages/2147483647")).status_code == 404
        assert (
            len((await client.get(f"/api/risk/news/{news_id}/packages")).json()["data"])
            == 1
        )
        assert (
            await client.get(f"/api/risk/news/{news_id}/packages?revision_no=99")
        ).json()["data"] == []
        async with SessionLocal() as s:
            published = dict(
                (
                    await s.execute(
                        select(SkillNode.id, SkillNode.published_version_id)
                    )
                ).all()
            )
        ids = (await client.post("/api/risk/skill-templates")).json()["data"]
        async with SessionLocal() as s:
            provider = AiProvider(
                name="ci-only-" + str(now.timestamp()),
                protocol="openai",
                base_url="https://example.invalid",
                api_key_encrypted=encrypt("ci-only"),
                api_key_hint="ci",
                model="ci-only",
                max_tokens=1000,
                context_window=100000,
                reasoning_effort=None,
            )
            s.add(provider)
            for identifier in ids.values():
                node = await s.get(SkillNode, identifier)
                assert (
                    node.scope == "news"
                    and node.published_version_id == published.get(identifier)
                )
                node.published_version_id = await s.scalar(
                    select(SkillVersion.id)
                    .where(SkillVersion.skill_id == identifier)
                    .order_by(SkillVersion.id.desc())
                    .limit(1)
                )
            await s.commit()
            provider_id = provider.id
        config = {
            stage: {
                "provider_id": provider_id,
                "model": "ci-only",
                "reasoning_effort": None,
                "skill_id": ids[stage],
            }
            for stage in ("index", "research")
        }
        config["concurrency"] = 2
        assert (await client.put("/api/risk/settings", json=config)).status_code == 200
        assert (await client.get("/api/risk/settings")).json()["data"][
            "enabled"
        ] is False
        async with SessionLocal() as s:
            for stage in ("index", "research"):
                provider, skill, rules = await service.model_config(s, config, stage)
                assert (
                    rules["skill_id"] == ids[stage]
                    and skill
                    and provider.model == "ci-only"
                )

        async def validation(provider, skill, payload, schema):
            source = payload.get("article") or payload["articles"][0]
            quote = source["content"]
            if schema is IndexResult:
                return schema(
                    keywords=[
                        {
                            "canonical": "示例公司",
                            "aliases": ["示例公司"],
                            "quote": quote,
                        }
                    ]
                ), {}
            if schema is Assignment:
                return schema(
                    targets=[
                        {
                            "event_id": None,
                            "new_key": "ci",
                            "title": "Test",
                            "reason": "Test",
                            "quote": quote,
                            "keyword_canonicals": ["示例公司"],
                            "change_kind": "new_fact",
                            "urgent": False,
                        }
                    ]
                ), {}
            result = dict(version.output)
            result.update(
                id=1,
                evidence_refs=[{"article_id": 1, "quote": quote}],
                timeline_points=[
                    {
                        **result["timeline_points"][0],
                        "package_ids": [1],
                        "evidence_refs": [{"article_id": 1, "quote": quote}],
                    }
                ],
            )
            return schema.model_validate(result), {}

        with patch.object(model_service, "complete", validation):
            response = await client.post("/api/risk/enable")
            assert response.status_code == 200, response.text
        assert (await client.get("/api/risk/settings")).json()["data"][
            "enabled"
        ] is True
        assert (await client.post("/api/risk/enable")).status_code == 409
        assert (await client.post("/api/risk/pause")).status_code == 200
        for action in ("pause", "save"):
            interrupted = False

            async def interrupted_validation(
                provider, skill, payload, schema, *, action=action
            ):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    response = (
                        await client.post("/api/risk/pause")
                        if action == "pause"
                        else await client.put("/api/risk/settings", json=config)
                    )
                    assert response.status_code == 200
                return await validation(provider, skill, payload, schema)

            with patch.object(model_service, "complete", interrupted_validation):
                response = await client.post("/api/risk/enable")
                assert response.status_code == 409, response.text
            assert (await client.get("/api/risk/settings")).json()["data"][
                "enabled"
            ] is False
        package_id = (await client.get(f"/api/risk/news/{news_id}/packages")).json()[
            "data"
        ][0]["id"]
        assert (
            await client.post(f"/api/risk/packages/{package_id}/retry")
        ).status_code == 200
        async with SessionLocal() as s:
            row = await s.get(Package, package_id)
            assert row.state == "assign_pending" and row.keywords
        event_detail = (await client.get(f"/api/risk/events/{event_id}")).json()["data"]
        assert (
            event_detail["packages"]
            and event_detail["versions"][0]["state"] == "failed"
        )
        assert (
            await client.post(f"/api/risk/events/{event_id}/retry")
        ).status_code == 200
        async with SessionLocal() as s:
            assert (await s.get(Event, event_id)).revision == version.revision
            package = await s.get(Package, package_id)
            package.state = "done"
            await s.commit()
            other_id = await s.scalar(
                select(Membership.event_id).where(
                    Membership.package_id == package_id, Membership.event_id != event_id
                )
            )

        async def success(self, provider, skill, payload, schema):
            if payload["id"] == event_id:
                cached = await self.tool("read_news", {"id": 2147483647})
                assert cached["content"] == "Previously verified background"
                assert self.counts["read"] == 0
                assert any(r.get("reused") for r in self.records)
            result = dict(version.output)
            quote = payload["articles"][0]["content"]
            result.update(
                id=payload["id"],
                evidence_refs=[{"article_id": news_id, "quote": quote}],
                references=[],
                timeline_points=[
                    {
                        **result["timeline_points"][0],
                        "package_ids": [package_id],
                        "evidence_refs": [{"article_id": news_id, "quote": quote}],
                    }
                ],
            )
            if payload["id"] == other_id:
                target = await self.tool("read_event", {"id": event_id})
                result["references"] = [
                    {
                        "version_id": target["version_id"],
                        "role": "background",
                        "reason": "Original source already provided",
                    }
                ]
            return schema.model_validate(result), {}

        with patch.object(service.Research, "run", success):
            await service.process_event(event_id, config, force=True)
            await service.process_event(other_id, config, force=True)
        detail = (await client.get(f"/api/risk/events/{other_id}")).json()["data"]
        assert detail["state"] == "done" and detail["references"][0]["changed"] is False
        assert (
            await client.post(f"/api/risk/events/{event_id}/retry")
        ).status_code == 200
        detail = (await client.get(f"/api/risk/events/{other_id}")).json()["data"]
        assert detail["state"] == "done" and detail["references"][0]["changed"] is True
        # Referenced changes do not automatically enqueue the referring event.

        # Old source failures cannot replace work for a newly enrolled source revision.
        async with SessionLocal() as s:
            changed_news = NewsItem(
                source_code="wscn_news",
                external_id="ci-source-race-" + str(now.timestamp()),
                title="Original incident and response",
                content_text="Original incident and response",
                content_hash="d" * 64,
                url="https://example.invalid/source-race",
                published_at=now,
            )
            s.add(changed_news)
            await s.commit()
            changed_news_id = changed_news.id
            await service.enroll(s, [changed_news_id])
            changed_package_id = await s.scalar(
                select(Package.id).where(Package.news_id == changed_news_id)
            )
        with patch.object(service, "complete", complete):
            await service.process_package(changed_package_id, config)

        async def old_request_failure(*args):
            async with SessionLocal() as s:
                changed_news = await s.get(NewsItem, changed_news_id)
                changed_news.content_text += " updated"
                changed_news.content_hash = "e" * 64
                changed_news.revision_no = 2
                await s.commit()
            response = await client.get(
                f"/api/risk/news/{changed_news_id}/packages?revision_no=2"
            )
            assert response.json()["data"] == []
            detail = (
                await client.get(f"/api/risk/packages/{changed_package_id}")
            ).json()["data"]
            assert not detail["keywords"] and not detail["events"]
            assert detail["state"] == "index_pending"
            async with SessionLocal() as s:
                await service.enroll(s, [changed_news_id])
            raise TimeoutError("Old source request timed out")

        with patch.object(service, "complete", old_request_failure):
            await service.process_package(changed_package_id, config)
        async with SessionLocal() as s:
            package = await s.get(Package, changed_package_id)
            assert package.content_hash == "e" * 64
            assert package.state == "index_pending" and package.error is None
            attempt = await s.scalar(
                select(Attempt)
                .where(Attempt.package_id == changed_package_id)
                .order_by(Attempt.id.desc())
                .limit(1)
            )
            assert attempt.state == "failed" and attempt.content_hash == "d" * 64
        with patch.object(service, "complete", complete):
            await service.process_package(changed_package_id, config)
        async with SessionLocal() as s:
            assert (await s.get(Package, changed_package_id)).state == "assign_pending"

        # Date-free titles still have bounded original evidence across a long history.
        async with SessionLocal() as s:
            for days in (180, 60, 5, 2, 1):
                published_at = now - timedelta(days=days)
                quote = f"{published_at.date()} Original incident and response"
                historical = NewsItem(
                    source_code="wscn_news",
                    external_id=f"ci-candidate-{days}-{now.timestamp()}",
                    title="Recurring report",
                    content_text=quote,
                    content_hash=str(days).zfill(64),
                    url="https://example.invalid/candidate",
                    published_at=published_at,
                )
                s.add(historical)
                await s.flush()
                package = Package(
                    news_id=historical.id,
                    content_hash=historical.content_hash,
                    state="done",
                    keywords=[],
                )
                s.add(package)
                await s.flush()
                s.add(
                    Membership(
                        event_id=event_id,
                        package_id=package.id,
                        evidence=quote,
                        reason="Same incident follow-up",
                        change_kind="new_fact",
                    )
                )
            await s.commit()
            event = await s.get(Event, event_id)
            context = (await service.candidate_context(s, [event]))[0]
            assert context["reporting_range"] == {
                "first": (now - timedelta(days=180)).isoformat(),
                "latest": now.isoformat(),
            }
            assert len(context["evidence"]) == 3
            assert [e["published_at"] for e in context["evidence"]] == [
                (now - timedelta(days=180)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
                now.isoformat(),
            ]
            assert (
                str((now - timedelta(days=180)).date())
                in context["evidence"][0]["quote"]
            )
            assert (await s.get(NewsItem, news_id)).content_hash == "c" * 64
    await review_regressions(config, complete, changed_package_id, changed_news_id)
    await enrollment_regressions()
    await phrase_search_regressions()
    await event_search_regressions()
    await compound_candidate_regressions()
    await vocabulary_regressions()
    await retrieval_revision_regressions()
    await membership_tag_regressions()
    await research_scope_regressions()
    await engine.dispose()
    print(
        "Two-stage smoke passed: indexing, membership, evidence reuse, pause cancellation, source races and dated event candidates."
    )


async def review_regressions(config, complete, package_id, news_id):
    # Publication must not overwrite another session's newer source enrollment.
    async def superseded_success(*args):
        result = await complete(*args)
        async with SessionLocal() as session:
            news = await session.get(NewsItem, news_id)
            news.content_text += " newer revision"
            news.content_hash = "f" * 64
            await session.commit()
            await service.enroll(session, [news_id])
        return result

    with patch.object(service, "complete", superseded_success):
        await service.process_package(package_id, config)
    async with SessionLocal() as session:
        package = await session.get(Package, package_id)
        assert (
            package.content_hash == "f" * 64
            and package.state == "index_pending"
            and package.keywords == []
        )
        attempt = await session.scalar(
            select(Attempt)
            .where(Attempt.package_id == package_id)
            .order_by(Attempt.id.desc())
            .limit(1)
        )
        assert attempt.state == "superseded"
        package.state = "assign_pending"
        await session.commit()
    with patch.object(service, "complete", complete):
        await service.process_package(package_id, config)
    async with SessionLocal() as session:
        package = await session.get(Package, package_id)
        assert package.state == "assign_failed" and "关键词" in package.error
        from alphapulse.news_research.api import retry_package

        await retry_package(package_id, session)
        assert package.state == "index_pending"
        # One canonical group cannot gain ranking weight through its aliases.
        old = Event(
            title="Specific old incident",
            tags=["binance", "hack"],
            updated_at=datetime.now(UTC) - timedelta(days=180),
        )
        session.add(old)
        session.add_all(
            [
                Event(
                    title=f"Recent company item {i}",
                    tags=["binance"],
                    updated_at=datetime.now(UTC),
                )
                for i in range(65)
            ]
        )
        package.keywords = [
            {"canonical": "币安", "aliases": ["Binance"], "quote": "Binance"}
        ]
        await session.commit()
        catalog = await service.candidates(
            session,
            [
                {"canonical": "binance", "aliases": ["BINANCE"]},
                {"canonical": "hack", "aliases": ["HACK"]},
            ],
        )
        assert len(catalog) == 60 and catalog[0].id == old.id
        for term in ("Binance", " binANCE ", "币安"):
            condition, score = service.package_matching([term])
            matches = list(
                await session.scalars(
                    select(Package.id)
                    .join(NewsItem)
                    .where(condition)
                    .order_by(score.desc())
                )
            )
            assert package_id in matches
        assert package.keywords[0]["aliases"] == ["Binance"]
    # Two simultaneous callers must classify against consecutive committed catalogs.
    package_ids = []
    async with SessionLocal() as session:
        for i in range(2):
            news = NewsItem(
                source_code="wscn_news",
                external_id=f"ci-serial-{datetime.now(UTC).timestamp()}-{i}",
                title="Serial incident",
                url="https://example.invalid/serial",
                content_text="serialincident frozen evidence",
                content_hash=str(i) * 64,
                published_at=datetime.now(UTC),
            )
            session.add(news)
            await session.flush()
            package = Package(news_id=news.id, content_hash=news.content_hash)
            session.add(package)
            await session.flush()
            package_ids.append(package.id)
        await session.commit()
    submissions = []

    async def serial_complete(provider, skill, payload, schema):
        submissions.append(schema.__name__)
        if schema is IndexResult:
            return schema(
                keywords=[
                    {
                        "canonical": "serialincident",
                        "aliases": ["serialincident"],
                        "quote": "serialincident",
                    }
                ]
            ), {}
        existing = next(
            (e for e in payload["events"] if e["title"] == "Serial incident"), None
        )
        await asyncio.sleep(0.01)
        return schema(
            targets=[
                {
                    "event_id": existing["id"] if existing else None,
                    "new_key": None if existing else "serial",
                    "title": "Serial incident",
                    "reason": "Same incident",
                    "quote": "serialincident frozen evidence",
                    "keyword_canonicals": ["serialincident"],
                    "change_kind": "new_fact",
                    "urgent": False,
                }
            ]
        ), {}

    with patch.object(service, "complete", serial_complete):
        await asyncio.gather(*(service.process_package(i, config) for i in package_ids))
        await asyncio.gather(*(service.process_package(i, config) for i in package_ids))
    assert (
        submissions.count("IndexResult") == 2 and submissions.count("Assignment") == 2
    )
    async with SessionLocal() as session:
        memberships = list(
            await session.scalars(
                select(Membership).where(Membership.package_id.in_(package_ids))
            )
        )
        assert len(memberships) == 2 and len({m.event_id for m in memberships}) == 1
        event_id = memberships[0].event_id
        template = (
            await session.scalar(
                select(Version)
                .where(Version.output.is_not(None))
                .order_by(Version.id)
                .limit(1)
            )
        ).output

    # Force compression, then exercise both member read tools with frozen originals.
    async def extract(provider, rules, articles):
        assert "当前事件及各成员归属边界" in rules
        assert '"keyword_canonicals": ["serialincident"]' in rules
        return [
            {
                "evidence_refs": [
                    {"article_id": a["id"], "quote": "serialincident"} for a in articles
                ]
            }
        ], []

    async def compact(provider, rules, request, schema, fits):
        return request, []

    async def compressed_run(self, provider, skill, payload, schema):
        assert payload["articles"] == [] and "extractions" in payload
        assert all(p["scope_selection"] == "assignment" and p["keyword_canonicals"] == ["serialincident"]
                   for p in payload["packages"])
        assert not self.provided_news
        refs = []
        for i, package in enumerate(payload["packages"]):
            result = await self.tool(
                "read_news" if i == 0 else "read_package",
                {"id": package["news_id"] if i == 0 else package["id"]},
            )
            assert (
                result["article_id"] == package["news_id"]
                and result["content"] == "serialincident frozen evidence"
            )
            refs.append(
                {"article_id": result["article_id"], "quote": result["content"]}
            )
        point = {
            **template["timeline_points"][0],
            "package_ids": payload["package_ids"],
            "evidence_refs": refs,
            "description": "serialincident",
            "time_text": "",
        }
        return schema(
            **{
                **template,
                "id": event_id,
                "evidence_refs": refs,
                "timeline_points": [point],
                "references": [],
            }
        ), {}

    with (
        patch.object(service.Research, "fits", return_value=False),
        patch.object(service, "extract_articles", extract),
        patch.object(service, "compact_extractions", compact),
        patch.object(service.Research, "run", compressed_run),
    ):
        await service.process_event(event_id, config, force=True)
    async with SessionLocal() as session:
        event = await session.get(Event, event_id)
        assert event.state == "done", event.error
    print("Serial assignment and compressed member tool integration passed.")
    print(
        "Review regressions passed: superseded success, durable empty-index failure, long-lived ranking and literal alias search."
    )


async def enrollment_regressions():
    for cache in ("news", "package", "both"):
        async with SessionLocal() as session:
            news = NewsItem(
                source_code="wscn_news",
                external_id=f"ci-cache-{cache}-{datetime.now(UTC).timestamp()}",
                title="Cache fixture",
                url="https://example.invalid/cache",
                content_text="Second revision",
                content_hash="2" * 64,
                published_at=datetime.now(UTC),
            )
            session.add(news)
            await session.flush()
            package = Package(
                news_id=news.id,
                content_hash=news.content_hash,
                state="done",
                keywords=[
                    {
                        "canonical": "Second",
                        "aliases": ["Second"],
                        "quote": "Second revision",
                    }
                ],
            )
            event = Event(title="Cache event", revision=7, state="done")
            session.add_all([package, event])
            await session.flush()
            session.add(
                Membership(
                    package_id=package.id,
                    event_id=event.id,
                    evidence="Second revision",
                    reason="Same event",
                    change_kind="new_fact",
                )
            )
            await session.commit()
            news_id, package_id, event_id = news.id, package.id, event.id
        async with SessionLocal() as stale:
            # Keep strong references so ORM identity-map entries remain cached.
            cached_news = (
                await stale.get(NewsItem, news_id)
                if cache in ("news", "both")
                else None
            )
            cached_package = (
                await stale.get(Package, package_id)
                if cache in ("package", "both")
                else None
            )
            async with SessionLocal() as fresh:
                news = await fresh.get(NewsItem, news_id)
                package = await fresh.get(Package, package_id)
                news.content_hash, news.content_text = "3" * 64, "Third revision"
                package.content_hash, package.state = (
                    news.content_hash,
                    "assign_processing",
                )
                package.keywords = [
                    {
                        "canonical": "Third",
                        "aliases": ["Third"],
                        "quote": "Third revision",
                    }
                ]
                await fresh.commit()
            await service.enroll(stale, [news_id])
            assert cached_news is not None or cached_package is not None
        async with SessionLocal() as session:
            package = await session.get(Package, package_id)
            event = await session.get(Event, event_id)
            assert (
                package.content_hash == "3" * 64
                and package.state == "assign_processing"
                and package.keywords[0]["canonical"] == "Third"
            ), cache
            assert event.revision == 7, cache
            news = await session.get(NewsItem, news_id)
            news.content_hash, news.content_text = "4" * 64, "Fourth revision"
            await session.commit()
            await service.enroll(session, [news_id])
            await session.refresh(package)
            await session.refresh(event)
            assert (
                package.content_hash == "4" * 64
                and package.state == "index_pending"
                and package.keywords == []
            )
            assert event.revision == 8
            await service.enroll(session, [news_id])
            await session.refresh(event)
            assert event.revision == 8
    print("Enrollment cached-source and cached-package regressions passed.")


async def phrase_search_regressions():
    async with SessionLocal() as session:
        target_id = None
        for i in range(23):
            news = NewsItem(
                source_code="wscn_news",
                external_id=f"ci-phrase-{datetime.now(UTC).timestamp()}-{i}",
                title="Generic report" if i == 0 else "Federal policy Reserve outlook",
                url="https://example.invalid/phrase",
                content_text="Federal Reserve announcement",
                content_hash="5" * 64,
                published_at=datetime.now(UTC),
            )
            session.add(news)
            await session.flush()
            package = Package(
                news_id=news.id,
                content_hash=news.content_hash,
                state="done",
                keywords=[
                    {
                        "canonical": "美联储",
                        "aliases": ["Federal Reserve"],
                        "quote": news.content_text,
                    }
                ]
                if i == 0
                else [],
            )
            session.add(package)
            await session.flush()
            if i == 0:
                target_id = package.id
        await session.commit()
    for args in (
        {"query": "Federal Reserve"},
        {"query": "  fEDERAL rESERVE  "},
        {
            "query": "Federal Reserve",
            "keywords": ["one", "two", "three", "four", "five", "six"],
        },
        {"query": "unrelated", "keywords": ["Federal Reserve"]},
        {
            "query": "absent",
            "keywords": ["one", "two", "three", "four", "five", "Federal Reserve"],
        },
        {"query": "美联储"},
    ):
        rows = await service.Research(event={"id": 0}).tool("search_packages", args)
        assert rows and rows[0]["id"] == target_id, args
        assert len(rows) <= 20
    for args in (
        {"query": "Federal policy"},
        {"query": "absent", "keywords": ["Federal"]},
    ):
        assert await service.Research(event={"id": 0}).tool("search_packages", args)
    async with SessionLocal() as session:
        package = await session.get(Package, target_id)
        assert package.keywords[0]["aliases"] == ["Federal Reserve"]
    print("Phrase retrieval and top-20 ranking regressions passed.")


async def event_search_regressions():
    async with SessionLocal() as session:
        target = Event(
            title="加拿大央行利率决议",
            tags=["bank of canada", "加拿大央行"],
            updated_at=datetime.now(UTC) - timedelta(days=180),
        )
        own = Event(title="Current event", tags=["bank of canada"])
        retired = Event(
            title="Retired event", tags=["bank of canada"], state="retired"
        )
        session.add_all([target, own, retired])
        session.add_all(
            Event(title=f"Bank lending of Canada {i}", tags=[]) for i in range(23)
        )
        await session.commit()
        target_id, own_id, retired_id = target.id, own.id, retired.id
    for args in (
        {"query": "Bank of Canada"},
        {"query": "  bANK oF cANADA  "},
        {
            "query": "Bank of Canada",
            "keywords": ["one", "two", "three", "four", "five", "six"],
        },
        {"query": "absent", "keywords": ["Bank of Canada"]},
        {"query": "加拿大央行"},
    ):
        rows = await service.Research(event={"id": own_id}).tool("search_events", args)
        assert rows and rows[0]["id"] == target_id, args
        assert len(rows) <= 20
        assert not {own_id, retired_id} & {row["id"] for row in rows}
    print("Event phrase retrieval, relevance ranking and visibility regressions passed.")



async def compound_candidate_regressions():
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        target = Event(title="Historical exchange incident", tags=["bybit"], updated_at=now-timedelta(days=180))
        title_only = Event(title="Bybit frozen-funds follow-up", tags=[])
        unrelated = Event(title="Bybitx unrelated announcement", tags=["bybitx"])
        retired = Event(title="Retired Bybit incident", tags=["bybit"], state="retired")
        currency = Event(title="USDC reserve", tags=["usdc"])
        exact = Event(title="Earlier full-phrase match", tags=["bybit hack"], updated_at=now-timedelta(days=400))
        chinese_old = Event(title="隔离中文候选", tags=[], updated_at=now-timedelta(days=10))
        chinese_new = Event(title="隔离中文候选", tags=[], updated_at=now)
        session.add_all([target, title_only, unrelated, retired, currency, exact, chinese_old, chinese_new])
        await session.flush()
        keys = [{"canonical": "Bybit黑客事件", "aliases": ["Bybit Hack"]}]
        rows = await service.candidates(session, keys)
        ids = {row.id for row in rows}
        assert {target.id, title_only.id, exact.id} <= ids
        assert not {unrelated.id, retired.id} & ids
        assert rows[0].id == exact.id
        rows = await service.candidates(session, [{"canonical": "美元抵押", "aliases": ["USD collateral"]}])
        assert currency.id not in {row.id for row in rows}
        rows = await service.candidates(session, [{"canonical": "隔离中文候选", "aliases": ["隔离中文候选"]}])
        assert [row.id for row in rows] == [chinese_new.id, chinese_old.id]
        session.add_all(Event(title=f"Recent Bybit update {i}", tags=["bybit"]) for i in range(65))
        await session.flush()
        rows = await service.candidates(session, keys)
        assert len(rows) == 60 and rows[0].id == exact.id
        await session.rollback()
    print("Compound-alias candidate recall, whole-token boundaries, ranking and sixty-result cap passed.")


async def vocabulary_regressions():
    from alphapulse.news_research import api
    now = datetime.now(UTC)
    # Vocabulary changes affect recall without rewriting prior evidence or paid work.
    from alphapulse.news_research import vocabulary as vocab
    from alphapulse.news_research.models import Reference
    async with SessionLocal() as session:
        saved = await api.save_vocabulary(vocab.VocabularyInput(base_revision=0, entries=[{"canonical": "币安", "aliases": ["Binance"]}]), session)
        assert saved.data["revision"] == 1
        old = Event(title="Historical incident A", tags=["binance", "incident-a"], revision=1, state="done", updated_at=now-timedelta(days=400))
        session.add(old)
        session.add_all([Event(title=f"Unrelated subject occurrence {i}", tags=["binance"], revision=1, state="done") for i in range(65)])
        story = NewsItem(source_code="wscn_news", external_id=f"ci-vocab-{now.timestamp()}", title="Original evidence", content_text="Binance incident-a", content_hash="f"*64, published_at=now-timedelta(days=400), url="https://example.invalid/vocab")
        session.add(story)
        await session.flush()
        package = Package(news_id=story.id, content_hash=story.content_hash, state="done", keywords=[{"canonical": "Binance", "aliases": ["Binance"], "quote": "Binance incident-a"}])
        session.add(package)
        await session.flush()
        frozen = service.article(story)
        target = Version(event_id=old.id, revision=1, state="done", input={"packages": [{"id": package.id, "news_id": story.id}], "articles": [frozen]}, output={"title": "Historical title", "summary": "Frozen summary"}, rules={})
        session.add(target)
        await session.commit()
        keys = [{"canonical": "币安", "aliases": ["Binance"]}, {"canonical": "incident-a", "aliases": ["incident-a"]}]
        assert (await service.candidates(session, keys))[0].id == old.id
        rows = await api.packages(session, q="币安", page=1)
        assert package.id in {row["id"] for row in rows.data["items"]}
        await api.save_vocabulary(vocab.VocabularyInput(base_revision=1, entries=[{"canonical": "币安", "aliases": ["Binance", "BNC"]}]), session)
        rows = await api.packages(session, q=" BNC ", page=1)
        assert package.id in {row["id"] for row in rows.data["items"]}
        assert package.keywords[0]["canonical"] == "Binance"
        snapshot = await api.event_version(old.id, target.id, session)
        assert snapshot.data["articles"][0]["content"] == "Binance incident-a"
        consumers = []
        for i in range(2):
            consumer = Event(title=f"Independent rule {i}", tags=[], revision=1, state="done")
            session.add(consumer)
            await session.flush()
            version = Version(event_id=consumer.id, revision=1, state="done", input={}, output={}, rules={})
            session.add(version)
            await session.flush()
            session.add(Reference(version_id=version.id, target_version_id=target.id, role="support", reason="Independent source confirms context"))
            consumers.append(version)
        await session.commit()
        old.title = "Current different title"
        service.dirty(old)
        story.content_text = "Updated original"
        await session.commit()
        for consumer in consumers:
            refs = await api.version_references(session, consumer.id)
            assert refs[0]["changed"] and refs[0]["title"] == "Historical title"
            assert refs[0]["version_id"] == target.id
        assert (await api.event_version(old.id, target.id, session)).data["articles"][0]["content"] == "Binance incident-a"
        assert (await session.get(Package, package.id)).state == "done"
        old_id = old.id
    results = await service.Research(event={"id": 0}).tool("search_events", {"query": "BNC", "keywords": ["incident-a"]})
    assert results[0]["id"] == old_id
    print("Vocabulary API, cross-year recall above 60 distractors, alias updates and two immutable references passed.")


async def retrieval_revision_regressions():
    from alphapulse.news_research import api, vocabulary as vocab

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        news = NewsItem(
            source_code="wscn_news",
            external_id=f"ci-stale-tags-{now.timestamp()}",
            title="Corrected headline for retrieval regression",
            content_text="Binance corrected company announcement",
            content_hash="8" * 64,
            published_at=now,
            url="https://example.invalid/retrieval-revision",
        )
        session.add(news)
        await session.flush()
        package = Package(
            news_id=news.id,
            content_hash="7" * 64,
            state="done",
            keywords=[{"canonical": "obsolete-retrieval-tag", "aliases": ["obsolete-retrieval-tag"], "quote": "obsolete-retrieval-tag"}],
        )
        session.add(package)
        await session.commit()
        package_id = package.id
        stale = await api.packages(session, q="obsolete-retrieval-tag", page=1)
        assert stale.data["total"] == 0
        current = await api.packages(session, q=news.title, page=1)
        assert current.data["items"][0]["id"] == package_id
        assert current.data["items"][0]["keywords"] == []
    query = {"query": "obsolete-retrieval-tag"}
    assert await service.Research(event={"id": 0}).tool("search_packages", query) == []
    current = await service.Research(event={"id": 0}).tool("search_packages", {"query": "Corrected headline for retrieval regression"})
    assert current[0]["id"] == package_id and current[0]["keywords"] == []
    async with SessionLocal() as session:
        package = await session.get(Package, package_id)
        package.content_hash = "8" * 64
        package.keywords = [{"canonical": "Binance", "aliases": ["Binance"], "quote": "Binance"}]
        current = await vocab.snapshot(session)
        before = {"revision": current["revision"], "entries": current["entries"]}
        entries = [{**e, "aliases": [*e["aliases"], "BNC-reviewed"]} if e["canonical"] == "币安" else e for e in current["entries"]]
        await api.save_vocabulary(vocab.VocabularyInput(base_revision=current["revision"], entries=entries), session)
        after = await vocab.snapshot(session)
        event = Event(title="Reviewed independent incident", tags=["binance"])
        session.add(event)
        await session.commit()
        event_id = event.id
    for tool, target in (("search_packages", package_id), ("search_events", event_id)):
        args = {"query": "BNC-reviewed", "keywords": []}
        prior = service.Research(event={"id": 0})
        prior.vocabulary = before
        assert await prior.tool(tool, args) == []
        retry = service.Research(event={"id": 0})
        retry.vocabulary = after
        retry.restore(prior.records)
        rows = await retry.tool(tool, args)
        assert target in {row["id"] for row in rows}
        assert len(retry.records) == 1 and not retry.records[0].get("reused")
    async with SessionLocal() as session:
        fresh = await api.packages(session, q="BNC-reviewed", page=1)
        assert package_id in {row["id"] for row in fresh.data["items"]}
    print("Stale TAG exclusion, current-title lookup and vocabulary retry refresh passed.")


async def membership_tag_regressions():
    original = "高盛公司警示美股风险。作者鲍比·莫拉维（Bobby Molavi）。民主党另有选情更新。"
    revised = "公司英伟达发布GPU。"
    followup = "Bobby Molavi补充材料支持原报告。"
    package_ids = []
    news_ids = []
    async with SessionLocal() as session:
        for i, content in enumerate((original, followup)):
            news = NewsItem(
                source_code="wscn_news", external_id=f"ci-membership-tags-{datetime.now(UTC).timestamp()}-{i}",
                title="Risk report" if i == 0 else "Follow-up", content_text=content,
                content_hash=str(i) * 64, published_at=datetime.now(UTC),
                url="https://example.invalid/membership-tags",
            )
            session.add(news)
            await session.flush()
            news_ids.append(news.id)
        await session.commit()
        await service.enroll(session, news_ids)
        for news_id in news_ids:
            package_ids.append(await session.scalar(select(Package.id).where(Package.news_id == news_id)))

    async def config(*args):
        return SimpleNamespace(context_window=1000000, max_tokens=1000), "test", {}

    event_id = None
    reject_revision = True

    async def complete(provider, skill, payload, schema):
        content = payload["article"]["content"]
        if schema is IndexResult:
            if content == original:
                pairs = [("高盛", ["高盛", "公司"]), ("鲍比·莫拉维", ["鲍比·莫拉维", "Bobby Molavi"]),
                         ("美国民主党", ["民主党"])]
            elif content == revised:
                pairs = [("英伟达", ["英伟达", "公司"])]
            else:
                pairs = [("原报告", ["原报告"]), ("鲍比·莫拉维", ["Bobby Molavi"])]
            return schema(keywords=[{"canonical": c, "aliases": a, "quote": content} for c, a in pairs]), {}
        if content == revised and reject_revision:
            raise ValueError("Synthetic assignment failure after reindexing")
        existing = event_id if content == followup else None
        return schema(targets=[{
            "event_id": existing, "new_key": None if existing else "report",
            "title": "高盛风险警示" if content == original else "GPU发布",
            "reason": "Same report" if existing else "Independent matter",
            "quote": "高盛公司警示美股风险。" if content == original else content,
            "keyword_canonicals": ["高盛", "鲍比·莫拉维"] if content == original else ["英伟达"] if content == revised else ["原报告", "鲍比·莫拉维"],
            "change_kind": "new_fact", "urgent": False,
        }]), {}

    with patch.object(service, "model_config", config), patch.object(service, "complete", complete):
        await service.process_package(package_ids[0], {})
        await service.process_package(package_ids[0], {})
        async with SessionLocal() as session:
            package = await session.get(Package, package_ids[0])
            assert package.state == "done", package.error
            event_id = await session.scalar(select(Membership.event_id).where(Membership.package_id == package.id))
            event = await session.get(Event, event_id)
            assert set(event.tags) == {"高盛", "公司", "鲍比·莫拉维", "bobby molavi"}
            catalog = await service.candidates(session, [{"canonical": "鲍比·莫拉维", "aliases": ["Bobby Molavi"]}])
            assert event_id in {e.id for e in catalog}, "Aliases outside the scope quote must recall the existing event"
            news = await session.get(NewsItem, news_ids[0])
            news.content_text, news.content_hash = revised, "9" * 64
            await session.commit()
            await service.enroll(session, [news.id])

        # Another assignment rebuilds this event after the revised index was published.
        await service.process_package(package_ids[0], {})
        await service.process_package(package_ids[1], {})
        await service.process_package(package_ids[1], {})
        await service.process_package(package_ids[0], {})
        async with SessionLocal() as session:
            other = await session.get(Package, package_ids[1])
            assert other.state == "done", other.error
            package = await session.get(Package, package_ids[0])
            assert package.state == "assign_failed" and package.keywords[0]["canonical"] == "英伟达"
            event = await session.get(Event, event_id)
            await service.refresh_event_tags(session, event)
            assert "bobby molavi" in event.tags and "英伟达" not in event.tags and "民主党" not in event.tags
            package.state = "assign_pending"
            await session.commit()

        # A failure after writing memberships must roll back snapshots and event creation.
        reject_revision = False
        with patch.object(service, "refresh_event_tags", side_effect=ValueError("Synthetic publication failure")):
            await service.process_package(package_ids[0], {})
        async with SessionLocal() as session:
            package = await session.get(Package, package_ids[0])
            assert package.state == "assign_failed" and package.error == "Synthetic publication failure"
            assert await session.scalar(select(Membership.event_id).where(Membership.package_id == package.id)) == event_id
            assert not await session.scalar(select(Event.id).where(Event.title == "GPU发布"))
            event = await session.get(Event, event_id)
            await service.refresh_event_tags(session, event)
            assert "bobby molavi" in event.tags and "英伟达" not in event.tags
            package.state = "assign_pending"
            await session.commit()

        # A successful reassignment removes only this member's old contribution.
        await service.process_package(package_ids[0], {})
        async with SessionLocal() as session:
            package = await session.get(Package, package_ids[0])
            assert package.state == "done", package.error
            remaining_tags = ["bobby molavi", "原报告", "鲍比·莫拉维"]
            assert (await session.get(Event, event_id)).tags == remaining_tags
            moved_id = await session.scalar(select(Membership.event_id).where(Membership.package_id == package.id))
            assert moved_id != event_id and set((await session.get(Event, moved_id)).tags) == {"公司", "英伟达"}
            # Legacy records use the historical input, even when the live index changes.
            legacy = await session.scalar(select(Attempt).where(
                Attempt.package_id == package_ids[1], Attempt.stage == "assign", Attempt.state == "done"))
            legacy.rules = {k: v for k, v in legacy.rules.items() if k != "membership_tags"}
            (await session.get(Package, package_ids[1])).keywords = package.keywords
            await session.flush()
            event = await session.get(Event, event_id)
            await service.refresh_event_tags(session, event)
            assert event.tags == remaining_tags
            await session.rollback()
    print("Membership TAG selection, English entity recall, revision interleaving and publication rollback passed.")


async def research_scope_regressions():
    content = "革命卫队公告称四艘油轮触雷。作者Ali提供事故说明。另有两艘船被拦截。"
    keywords = [
        {"canonical": "油轮触雷", "aliases": ["油轮触雷"], "quote": "四艘油轮触雷"},
        {"canonical": "Ali", "aliases": ["Ali"], "quote": "作者Ali提供事故说明"},
        {"canonical": "船舶拦截", "aliases": ["船被拦截"], "quote": "两艘船被拦截"},
    ]
    async with SessionLocal() as session:
        news = NewsItem(source_code="wscn_news", external_id=f"ci-research-scope-{datetime.now(UTC).timestamp()}",
                        title="Two independent incidents", content_text=content, content_hash="8" * 64,
                        url="https://example.invalid/research-scope", published_at=datetime.now(UTC))
        session.add(news)
        await session.commit()
        news_id = news.id
        await service.enroll(session, [news_id])
        package_id = await session.scalar(select(Package.id).where(Package.news_id == news_id))
        template = (await session.scalar(select(Version).where(Version.output.is_not(None)).order_by(Version.id).limit(1))).output

    targets = [
        {"event_id": None, "new_key": "mine", "title": "油轮触雷", "reason": "触雷事故",
         "quote": "革命卫队公告称四艘油轮触雷。", "keyword_canonicals": ["油轮触雷", "Ali"],
         "change_kind": "new_fact", "urgent": False},
        {"event_id": None, "new_key": "interception", "title": "船舶拦截", "reason": "独立拦截行动",
         "quote": "另有两艘船被拦截。", "keyword_canonicals": ["船舶拦截"],
         "change_kind": "new_fact", "urgent": False},
    ]

    async def config(*args):
        return SimpleNamespace(context_window=1000000, max_tokens=1000), "test", {}

    async def complete(provider, skill, payload, schema):
        return (schema(keywords=keywords) if schema is IndexResult else schema(targets=targets)), {}

    with patch.object(service, "model_config", config), patch.object(service, "complete", complete):
        await service.process_package(package_id, {})
        await service.process_package(package_id, {})
    async with SessionLocal() as session:
        event_ids = list(await session.scalars(select(Membership.event_id).where(Membership.package_id == package_id).order_by(Membership.event_id)))
        assert len(event_ids) == 2
        assignment = await session.scalar(select(Attempt).where(Attempt.package_id == package_id, Attempt.stage == "assign", Attempt.state == "done"))
        assert assignment.rules["membership_targets"] == {str(event_id): target for event_id, target in zip(event_ids, targets, strict=True)}
        # A newer failed assignment and mutable index must not replace the snapshot.
        session.add(Attempt(package_id=package_id, content_hash="8" * 64, stage="assign", state="failed",
                            input={"keywords": []}, rules={}, output=None))
        (await session.get(Package, package_id)).keywords = [{"canonical": "Unrelated", "aliases": ["Unrelated"]}]
        await session.commit()

    seen = []

    async def run(self, provider, skill, payload, schema):
        index = event_ids.index(payload["id"])
        target, other = targets[index], targets[1-index]
        package = payload["packages"][0]
        assert package["keyword_canonicals"] == target["keyword_canonicals"]
        assert package["keywords"] == [k for k in keywords if k["canonical"] in target["keyword_canonicals"]]
        assert package["other_scopes"] == [{"title": other["title"], "scope_quote": other["quote"], "reason": other["reason"]}]
        assert payload["articles"][0]["content"] == content
        seen.append(deepcopy(payload))
        refs = [{"article_id": news_id, "quote": target["quote"]}]
        point = {**template["timeline_points"][0], "package_ids": [package_id], "evidence_refs": refs,
                 "description": target["title"], "occurred_at": None, "time_precision": "unknown", "time_text": ""}
        return schema(**{**template, "id": payload["id"], "title": target["title"], "summary": target["title"],
                         "facts": target["quote"],
                         "claims": [{"statement": target["quote"], "verdict": "supported", "evidence_refs": refs}],
                         "evidence_refs": refs, "references": [],
                         "timeline_points": [point]}), {}

    with patch.object(service, "model_config", config), patch.object(service.Research, "run", run):
        for event_id in event_ids:
            await service.process_event(event_id, {}, force=True)
    async with SessionLocal() as session:
        for event_id, payload in zip(event_ids, seen, strict=True):
            event = await session.get(Event, event_id)
            assert event.state == "done", event.error
            version = await session.scalar(select(Version).where(Version.event_id == event_id))
            assert version.input == payload and version.state == "done"
        assert len(seen) == 2
        # Missing ownership fails durably before calling the model.
        assignment = await session.get(Attempt, assignment.id)
        assignment.state = "failed"
        event = await session.get(Event, event_ids[0])
        service.dirty(event)
        await session.commit()
    with patch.object(service, "model_config", side_effect=AssertionError("No model without snapshot")):
        await service.process_event(event_ids[0], {}, force=True)
    async with SessionLocal() as session:
        event = await session.get(Event, event_ids[0])
        assert event.state == "failed" and "快照" in event.error
        assert event.result is not None
        version = await session.scalar(select(Version).where(Version.event_id == event.id).order_by(Version.id.desc()).limit(1))
        assert version.state == "failed" and "快照" in version.error
        assignment = await session.get(Attempt, assignment.id)
        assignment.state, assignment.content_hash = "done", "9" * 64
        event.state = "pending"
        await session.commit()
    with patch.object(service, "model_config", side_effect=AssertionError("No model with stale snapshot")):
        await service.process_event(event_ids[0], {}, force=True)
    async with SessionLocal() as session:
        event = await session.get(Event, event_ids[0])
        assert event.state == "failed" and "版本不一致" in event.error
    print("Research receives immutable event-specific TAGs, sibling boundaries and full originals; missing snapshots fail durably.")


asyncio.run(main())
