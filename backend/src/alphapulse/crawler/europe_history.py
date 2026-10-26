"""欧洲核心宏观公报历史摘要预览；可导出供 import_history 显式导入，始终不写数据库。

uv run python -m alphapulse.crawler.europe_history SOURCE --start 2025-01-01
加 --output CAPTURES.jsonl.gz 保存真实响应和摘要；不覆盖已有文件。
这里只核对核心统计/利率公报，不宣称覆盖央行全部历史讲话、采访和纪要。
"""
import argparse
import asyncio
import base64
import gzip
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from parsel import Selector

from alphapulse.calendar.backfill import Document, LOCAL, family_for, fetch_documents
from alphapulse.calendar.europe import parse_announcement
from alphapulse.calendar.parsers import compact
from alphapulse.core.official_sources import FEEDS, official_url
from alphapulse.crawler.import_history import ArchivedArticle, Capture


def capture(document: Document) -> Capture:
    family = family_for(document)
    if family is None:
        raise ValueError("归档响应不是已支持的核心宏观公报")
    announcement = parse_announcement(family, document.body.decode())
    s = Selector(text=document.body.decode())
    title = compact(" ".join(s.css("h1").xpath("string(.)").getall()))
    # 仅摘取官网实际公开的导语；无导语保留空摘要，不拼凑新闻正文。
    selectors = {"ecb": "main p:not(.ecb-publicationDate)", "boe": ".page-content p", "eurostat": ".component-euind-content p",
                 "ons": "#main-points li", "destatis": "#content p", "insee": ".corps-publication p.paragraphe"}
    paragraphs = [compact(p.xpath("string(.)").get()) for p in s.css(selectors[document.source])]
    summary = next((p for p in paragraphs if p), "")
    url = official_url(document.source, document.url)
    article = ArchivedArticle(guid=url, url=url, title=title, summary=summary, published_at=announcement.at,
                              language="fr" if document.source == "insee" else "en")
    return Capture(source=document.source, url=url, status=200, headers=document.headers,
                   body=base64.b64encode(document.body).decode(), body_encoding="base64",
                   fetched_at=document.captured_at, articles=[article])


async def run(source: str, start: date, end: date, output: Path | None = None) -> dict:
    errors = []
    documents = await fetch_documents(start, end, set(), errors, sources={source})
    begin = datetime.combine(start, time.min, LOCAL).astimezone(UTC)
    finish = datetime.combine(end + timedelta(days=1), time.min, LOCAL).astimezone(UTC)
    captures = []
    for document in documents:
        try:
            item = capture(document)
            if begin <= item.articles[0].published_at < finish:
                captures.append(item)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append({"source": source, "url": document.url, "error": f"{type(exc).__name__}: {exc}"})
    if output is not None:
        with gzip.open(output, "xt", encoding="utf-8") as file:
            for item in captures:
                file.write(item.model_dump_json() + "\n")
    return {"mode": "export" if output else "preview", "source": source, "start": str(start), "end": str(end),
            "scope": "核心宏观统计/利率公报；不包括央行全部历史讲话、采访和纪要。首末日期不代表完整覆盖。",
            "documents": len(documents), "articles": len(captures),
            "published_start": min((c.articles[0].published_at.isoformat() for c in captures), default=None),
            "published_end": max((c.articles[0].published_at.isoformat() for c in captures), default=None),
            "output": str(output) if output else None, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", choices=sorted(FEEDS))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(LOCAL).date())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.start < date(2025, 1, 1) or args.end < args.start or args.end > datetime.now(LOCAL).date():
        parser.error("公布日期范围必须从 2025-01-01 起，结束日期不得晚于今天")
    if args.output is not None and args.output.exists():
        parser.error("输出文件已存在，请使用新路径，避免覆盖既有采集响应")
    report = asyncio.run(run(args.source, args.start, args.end, args.output))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
