import base64
import json
from datetime import datetime
from html import escape

from scrapy import Request
from scrapy.http import Response

from alphapulse.crawler.spiders.base import NewsSpider, ts

API = "https://api-one-wscn.awtmt.com/apiv1/content"


def _api_data(body: dict) -> dict:
    if body["code"] != 20000:
        raise ValueError(f"华尔街见闻接口业务错误（code={body['code']}）：{body['message']}")
    return body["data"]


class WscnNewsSpider(NewsSpider):
    """华尔街见闻·文章：列表只有摘要，新条目或标题/摘要变化时再拉详情"""

    name = "wscn_news"

    def first_requests(self) -> list[Request]:
        return [self.list_request("", 1)]

    def list_request(self, cursor: str, page: int, **meta) -> Request:
        return self.request(f"{API}/information-flow?channel=global&accept=article&limit=20&cursor={cursor}", self.parse_list, page=page, **meta)

    def history_request(self, before: datetime) -> Request:
        # 信息流 cursor 是 base64 的 JSON，ArticleLe 为文章时间上界（秒）
        cursor = json.dumps({"SlotOffset": 0, "TotalCount": 0, "ArticleLe": int(before.timestamp())}, separators=(",", ":"))
        return self.list_request(base64.b64encode(cursor.encode()).decode(), 1, history=True)

    async def parse_list(self, response: Response):
        data = _api_data(response.json())
        # 信息流里混有快讯，快讯由 wscn_live 负责
        rows = [x["resource"] for x in data["items"] if x["resource_type"] == "article"]
        ids = [str(r["id"]) for r in rows]
        known = await self.known_items(ids)
        for r in rows:
            if known.get(str(r["id"])) != (r["title"], r["content_short"]):
                listed = {key: r[key] for key in ("id", "uri", "title", "display_time", "is_priced")}
                listed["author"] = (r.get("author") or {}).get("display_name")
                listed["source_name"] = r.get("source_name")
                yield self.request(
                    f"{API}/articles/{r['id']}?extract=0", self.parse_detail,
                    summary=r["content_short"], listed_article=listed,
                    list_raw_response_id=response.meta["raw_response_id"], history=response.meta.get("history", False),
                )
        page = response.meta["page"]
        oldest = ts(min(r["display_time"] for r in rows)) if rows else None
        if response.meta.get("history"):
            if self.history_step(oldest):
                yield self.list_request(data["next_cursor"], page + 1, history=True)
        elif rows and self.should_continue(ids, known, oldest, page):
            yield self.list_request(data["next_cursor"], page + 1)

    async def parse_detail(self, response: Response):
        body = response.json()
        listed = response.meta["listed_article"]
        if body["code"] == 50000 and response.meta["history"] and listed["is_priced"]:
            # 旧付费课程的详情和官网页面均可能失效；保存列表公开信息，不伪装成已取得正文。
            self.logger.warning("历史付费文章 %s 详情接口失败（code=%s）：%s；仅保存列表公开摘要",
                                listed["id"], body["code"], body["message"])
            summary = response.meta["summary"]
            item = self.item(
                response, external_id=str(listed["id"]), url=listed["uri"], title=listed["title"],
                summary=summary, content_html=f"<p>{escape(summary)}</p>",
                published_at=ts(listed["display_time"]), author=listed["author"], is_paywalled=True,
                extra={"source_name": listed["source_name"], "is_priced": True,
                       "content_scope": "public_summary", "historical_backfill": True,
                       "detail_error": {"code": body["code"], "message": body["message"],
                                        "raw_response_id": response.meta["raw_response_id"]}},
            )
            item["raw_response_id"] = response.meta["list_raw_response_id"]
            yield item
            return
        a = _api_data(body)
        yield self.item(
            response,
            external_id=str(a["id"]),
            url=listed["uri"],
            title=a["title"],
            summary=response.meta["summary"],
            content_html=a["content"],
            published_at=ts(a["display_time"]),
            author=(a.get("author") or {}).get("display_name"),
            is_paywalled=bool(a.get("is_need_pay")),
            extra={"source_name": a.get("source_name"), "is_priced": a.get("is_priced")},
        )


class WscnLiveSpider(NewsSpider):
    """华尔街见闻·快讯：列表即全文"""

    name = "wscn_live"

    def first_requests(self) -> list[Request]:
        return [self.list_request("", 1)]

    def list_request(self, cursor: str, page: int, **meta) -> Request:
        return self.request(f"{API}/lives?channel=global-channel&limit=50&cursor={cursor}", self.parse_list, page=page, **meta)

    def history_request(self, before: datetime) -> Request:
        # cursor 为时间上界（秒）；接口只保留约最近 180 天，更早返回空
        return self.list_request(str(int(before.timestamp())), 1, history=True)

    async def parse_list(self, response: Response):
        data = _api_data(response.json())
        rows = data["items"]
        # 先查已入库集合，再产出条目：否则管道可能先落库，导致误判为没有新条目而停止翻页
        ids = [str(x["id"]) for x in rows]
        known = await self.known_items(ids)
        for x in rows:
            yield self.item(
                response,
                external_id=str(x["id"]),
                url=x["uri"],
                title=x["title"],
                summary="",
                content_html=x["content"] + (x.get("content_more") or ""),
                published_at=ts(x["display_time"]),
                author=(x.get("author") or {}).get("display_name"),
                tags=x.get("channels"),
                extra={"score": x.get("score"), "symbols": x.get("symbols")},
            )
        page = response.meta["page"]
        oldest = ts(min(x["display_time"] for x in rows)) if rows else None
        if response.meta.get("history"):
            if self.history_step(oldest):
                yield self.list_request(data["next_cursor"], page + 1, history=True)
        elif rows and self.should_continue(ids, known, oldest, page):
            yield self.list_request(data["next_cursor"], page + 1)
