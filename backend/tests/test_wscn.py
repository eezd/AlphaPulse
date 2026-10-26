import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from scrapy.http import Request, TextResponse


@pytest.fixture
def wscn(trading):
    from alphapulse.crawler.spiders.base import RunState
    from alphapulse.crawler.spiders.wscn import WscnNewsSpider

    spider = WscnNewsSpider()
    now = datetime(2026, 10, 22, tzinfo=UTC)
    spider.run = RunState(uuid.uuid4(), "schedule", now, now - timedelta(hours=72),
                          history_target=datetime(2025, 1, 1, tzinfo=UTC),
                          history_position=datetime(2025, 12, 10, tzinfo=UTC))
    return spider


def json_response(request, body, raw_id):
    request.meta["raw_response_id"] = raw_id
    return TextResponse(request.url, request=request, body=json.dumps(body).encode(), encoding="utf-8")


async def detail_request(spider, monkeypatch, *, history=True, priced=True):
    async def known_items(ids):
        return {}
    monkeypatch.setattr(spider, "known_items", known_items)
    response = json_response(spider.list_request("", 1, history=history), {
        "code": 20000, "data": {"items": [{"resource_type": "article", "resource": {
            "id": 3759372, "uri": "https://wallstreetcn.com/premium/articles/3759372?layout=wscn-layout",
            "title": "公开课程标题", "content_short": "公开摘要 <script> is text & data",
            "display_time": 1765281395, "is_priced": priced,
            "author": {"display_name": "公开作者"}, "source_name": "公开来源",
        }}], "next_cursor": "next-page"},
    }, 41)
    requests = [request async for request in spider.parse_list(response)]
    return next(request for request in requests if "/articles/3759372?" in request.url)


def test_historical_paid_error_keeps_list_summary_and_both_raw_responses(wscn, monkeypatch, caplog):
    async def check():
        request = await detail_request(wscn, monkeypatch)
        response = json_response(request, {"code": 50000, "message": "内部错误, 请稍后重试", "data": {}}, 42)
        item, = [item async for item in wscn.parse_detail(response)]
        assert item["external_id"] == "3759372"
        assert item["url"] == request.meta["listed_article"]["uri"]
        assert item["title"] == "公开课程标题" and item["author"] == "公开作者"
        assert item["summary"] == item["content_text"] == request.meta["summary"]
        assert "<script>" not in item["content_html"] and "&lt;script&gt;" in item["content_html"]
        assert item["published_at"] == datetime.fromtimestamp(1765281395, UTC)
        assert item["is_paywalled"] is True
        assert item["extra"]["content_scope"] == "public_summary"
        assert item["extra"]["historical_backfill"] is True
        assert item["extra"]["detail_error"] == {"code": 50000, "message": "内部错误, 请稍后重试", "raw_response_id": 42}
        assert item["raw_response_id"] == 41
        assert not wscn.run.errors
        assert wscn.run.history_position == item["published_at"]
    asyncio.run(check())
    assert "50000" in caplog.text and "内部错误" in caplog.text and "仅保存列表公开摘要" in caplog.text


@pytest.mark.parametrize("handler", ["article_list", "article_detail", "live_list"])
def test_http_200_business_error_reports_upstream_cause(wscn, monkeypatch, handler):
    from alphapulse.crawler.spiders.wscn import WscnLiveSpider

    async def check():
        if handler == "article_detail":
            request = await detail_request(wscn, monkeypatch, history=False)
            callback = wscn.parse_detail
        elif handler == "article_list":
            request = wscn.list_request("", 1)
            callback = wscn.parse_list
        else:
            request = Request("https://example.invalid/live")
            callback = WscnLiveSpider().parse_list
        response = json_response(request, {"code": 50000, "message": "内部错误, 请稍后重试", "data": {}}, 42)
        with pytest.raises(ValueError, match="code=50000.*内部错误"):
            _ = [item async for item in callback(response)]
    asyncio.run(check())


@pytest.mark.parametrize("priced,code", [(False, 50000), (True, 60327)])
def test_public_article_or_other_business_error_does_not_become_summary(wscn, monkeypatch, priced, code):
    async def check():
        request = await detail_request(wscn, monkeypatch, priced=priced)
        response = json_response(request, {"code": code, "message": "上游错误", "data": {}}, 42)
        with pytest.raises(ValueError, match=f"code={code}.*上游错误"):
            _ = [item async for item in wscn.parse_detail(response)]
    asyncio.run(check())


@pytest.mark.parametrize("priced", [False, True])
def test_successful_detail_preserves_body_and_uses_official_list_link(wscn, monkeypatch, priced):
    async def check():
        request = await detail_request(wscn, monkeypatch, priced=priced)
        response = json_response(request, {"code": 20000, "data": {
            "id": 3759372, "title": "详情标题", "content": "<p>详情公开正文</p>",
            "display_time": 1765281395, "author": {"display_name": "详情作者"},
            "is_need_pay": priced, "is_priced": priced, "source_name": "详情来源",
        }}, 42)
        item, = [item async for item in wscn.parse_detail(response)]
        assert item["content_html"] == "<p>详情公开正文</p>" and item["content_text"] == "详情公开正文"
        assert item["title"] == "详情标题" and item["author"] == "详情作者"
        assert item["is_paywalled"] is priced
        assert item["url"] == request.meta["listed_article"]["uri"] and item["raw_response_id"] == 42
        assert "content_scope" not in item["extra"] and "detail_error" not in item["extra"]
    asyncio.run(check())
