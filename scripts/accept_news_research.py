"""Bounded two-stage acceptance in an empty disposable CI database only."""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse
from sqlalchemy import select, func
from alphapulse.ai import llm
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem
from alphapulse.news_research import service
from alphapulse.news_research.models import (
    Package,
    Attempt,
    Event,
    Version,
    Membership,
    Reference,
)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, path, limit=240):
        self.path, self.limit = path, limit

    def take(self):
        with self.path.open("a+") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            count = int(file.read() or "0")
            if count >= self.limit:
                raise BudgetExceeded(f"真实模型请求已达{self.limit}次上限")
            file.seek(0)
            file.truncate()
            file.write(str(count + 1))
            file.flush()
            os.fsync(file.fileno())

    def wrap(self, original):
        async def stream(*args, **kwargs):
            self.take()
            async for event in original(*args, **kwargs):
                yield event

        return stream


async def main(args):
    if args.output.exists():
        raise ValueError("报告已存在，请使用新路径，保留既有验收记录")
    url = urlparse(os.environ["DATABASE_URL"])
    if not (
        url.username == "ci"
        and url.password == "ci-only"
        and url.path == "/ci"
        and url.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("只能写入独立本地CI数据库")
    raw = args.sample.read_bytes()
    source = json.loads(raw)
    news = source["news"] if args.synthetic else source["original_news"]
    if len(news) != (12 if args.synthetic else 30):
        raise ValueError("样本数量不符合冻结范围")
    credentials = json.loads(args.credentials.read_text())
    config = {"isolated_acceptance": True, "concurrency": 3}
    async with SessionLocal() as session:
        if await session.scalar(select(func.count()).select_from(NewsItem)):
            raise ValueError("验收数据库必须为空")
        for stage in ("index", "research"):
            fields = credentials[stage]
            provider = AiProvider(
                name="accept-" + stage,
                protocol=fields["protocol"],
                base_url=fields["base_url"],
                api_key_encrypted=encrypt(fields["api_key"]),
                api_key_hint="isolated",
                model=fields["model"],
                max_tokens=fields["max_tokens"],
                context_window=fields["context_window"],
            )
            session.add(provider)
            await session.flush()
            config[stage] = {
                "provider_id": provider.id,
                "model": "gpt-6.1-terra",
                "reasoning_effort": "medium" if stage == "index" else "xhigh",
                "skill_id": None,
            }
        columns = set(NewsItem.__table__.columns.keys())
        for item in news:
            values = {k: v for k, v in item.items() if k in columns}
            for k, v in list(values.items()):
                if k.endswith("_at") and v:
                    values[k] = datetime.fromisoformat(v)
            session.add(NewsItem(**values))
        await session.commit()
    budget = Budget(args.budget)
    batches = (
        source["batches"]
        if args.synthetic
        else [
            [n["id"] for n in sorted(news, key=lambda n: (n["published_at"], n["id"]))]
        ]
    )
    semaphore = asyncio.Semaphore(3)
    checkpoints = []

    async def indexed(identifier):
        async with semaphore:
            await service.process_package(identifier, config)

    async def researched(identifier):
        async with semaphore:
            await service.process_event(identifier, config, force=True)

    with ExitStack() as stack:
        for name in ("_stream_openai", "_stream_anthropic"):
            stack.enter_context(
                patch.object(llm, name, budget.wrap(getattr(llm, name)))
            )
        for batch in batches:
            if args.budget.exists() and int(args.budget.read_text() or "0") >= 240:
                break
            async with SessionLocal() as session:
                await service.enroll(session, batch)
                packages = list(
                    await session.scalars(
                        select(Package).where(Package.news_id.in_(batch))
                    )
                )
                ordered = {p.news_id: p.id for p in packages}
            await asyncio.gather(*(indexed(ordered[n]) for n in batch))
            for n in batch:
                await service.process_package(ordered[n], config)
            async with SessionLocal() as session:
                pending = list(
                    await session.scalars(
                        select(Event.id).where(Event.state == "pending")
                    )
                )
            await asyncio.gather(*(researched(i) for i in pending))
            async with SessionLocal() as session:
                checkpoints.append(
                    {
                        "batch": batch,
                        "versions": [
                            (v.event_id, v.id, v.revision, v.state)
                            for v in await session.scalars(select(Version))
                        ],
                    }
                )
            print(
                json.dumps(
                    {
                        "group": "synthetic" if args.synthetic else "real",
                        "batch": batch,
                        "requests": int(args.budget.read_text()),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    async with SessionLocal() as session:
        tables = {}
        for model in (
            Package,
            Attempt,
            Event,
            Version,
            Membership,
            Reference,
        ):
            tables[model.__tablename__] = [
                {c.name: getattr(row, c.name) for c in model.__table__.columns}
                for row in await session.scalars(select(model))
            ]
    packages = tables["news_research_packages"]
    memberships = tables["news_research_memberships"]
    links = {
        p["news_id"]: {m["event_id"] for m in memberships if m["package_id"] == p["id"]}
        for p in packages
    }
    for item in news:
        links.setdefault(item["id"], set())
    assertions = []
    must_link = (
        source["must_link"]
        if args.synthetic
        else [
            [463197, 463198, 463199, 463205],
            [463206, 463208, 463218],
            [463192, 463193],
            [463201, 463202],
            [463200, 463204],
            [463209, 463212],
            [463194, 463213],
            [463216, 463220],
        ]
    )
    for group in must_link:
        assertions.append(
            {
                "name": "same_event",
                "news_ids": group,
                "passed": bool(set.intersection(*(links[n] for n in group))),
            }
        )
    if args.synthetic:
        from itertools import combinations

        for a, b in combinations(source["must_separate"], 2):
            assertions.append(
                {
                    "name": "different_events",
                    "news_ids": [a, b],
                    "passed": not (links[a] & links[b]),
                }
            )
        versions = {v["id"]: v for v in tables["news_research_versions"]}
        for a, b in source["references"]:
            matching = [
                ref
                for ref in tables["news_research_references"]
                if versions[ref["version_id"]]["event_id"] in links[a]
                and versions[ref["target_version_id"]]["event_id"] in links[b]
            ]
            assertions.append(
                {
                    "name": "immutable_reference",
                    "news_ids": [a, b],
                    "passed": bool(matching)
                    and all(
                        versions[ref["target_version_id"]]["state"] == "done"
                        and versions[ref["target_version_id"]]["id"]
                        < max(
                            v["id"]
                            for v in versions.values()
                            if v["event_id"] in links[b] and v["state"] == "done"
                        )
                        for ref in matching
                    ),
                }
            )
        main_events = links[1001]
        good = [
            v
            for v in versions.values()
            if v["event_id"] in main_events and v["state"] == "done"
        ]
        assertions.append(
            {"name": "three_incremental_versions", "passed": len(good) == 3}
        )
        assertions.append(
            {
                "name": "repost_no_research",
                "passed": len(checkpoints) >= 2
                and checkpoints[0]["versions"] == checkpoints[1]["versions"],
            }
        )
    if not args.synthetic:
        from itertools import combinations

        for a, b in combinations([463194, 463200, 463203, 463209, 463214], 2):
            assertions.append(
                {
                    "name": "independent_foreign_ministry_topics",
                    "news_ids": [a, b],
                    "passed": not (links[a] & links[b]),
                }
            )
    report = {
        "quality_accepted": False,
        "automated_passed": all(a["passed"] for a in assertions)
        and all(p["state"] == "done" for p in packages)
        and all(e["state"] == "done" for e in tables["news_research_events"]),
        "human_review_required": [
            "事实与边界",
            "时间精度与统计期",
            "评级与冲突保留",
            "引用原文证据",
        ],
        "sample_sha256": hashlib.sha256(raw).hexdigest(),
        "group": "synthetic" if args.synthetic else "real",
        "original_news": news,
        "model_choices": {
            k: {f: v for f, v in config[k].items() if f != "provider_id"}
            for k in ("index", "research")
        },
        "request_count_total": int(args.budget.read_text()),
        "request_cap": 240,
        "not_completed_news_ids": [
            n["id"]
            for n in news
            if not any(
                p["news_id"] == n["id"] and p["state"] == "done" for p in packages
            )
        ],
        "budget_blocked": {
            name: [
                row["id"]
                for row in tables[name]
                if "真实模型请求已达240次上限" in (row.get("error") or "")
            ]
            for name in ("news_research_attempts", "news_research_versions")
        },
        "failed_processing": {
            name: [
                row["id"]
                for row in tables[name]
                if row["state"] == "failed"
                and "真实模型请求已达240次上限" not in (row.get("error") or "")
            ]
            for name in ("news_research_attempts", "news_research_versions")
        },
        "assertions": assertions,
        "checkpoints": checkpoints,
        "tables": tables,
        "usage_note": "缺失usage不等于零消耗；请求数不是费用；人工复核后才可质量放行。",
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    args.output.chmod(0o600)
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--sample", type=Path)
    inputs.add_argument("--scenario-manifest", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--finish", action="store_true")
    parser.add_argument("--request-limit", type=int)
    parser.add_argument("--repair-references", action="store_true")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args()
    if args.scenario_manifest:
        from accept_news_scenario import run
        asyncio.run(run(args))
    else:
        if args.resume or args.finish or args.request_limit is not None:
            parser.error("续跑选项仅用于场景验收")
        asyncio.run(main(args))
