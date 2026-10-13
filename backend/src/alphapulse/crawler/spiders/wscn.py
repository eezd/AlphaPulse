from scrapy import Request
from scrapy.http import Response

from alphapulse.crawler.spiders.base import NewsSpider, ts

API = "https://api-one-wscn.awtmt.com/apiv1/content"


class WscnNewsSpider(NewsSpider):
    """华尔街见闻·文章：列表只有摘要，新条目或标题/摘要变化时再拉详情"""

    name = "wscn_news"

    def first_requests(self) -> list[Request]:
        return [self.list_request("", 1)]

    def list_request(self, cursor: str, page: int) -> Request:
        return self.request(f"{API}/information-flow?channel=global&accept=article&limit=20&cursor={cursor}", self.parse_list, page=page)

    async def parse_list(self, response: Response):
        data = response.json()["data"]
        # 信息流里混有快讯，快讯由 wscn_live 负责
        rows = [x["resource"] for x in data["items"] if x["resource_type"] == "article"]
        ids = [str(r["id"]) for r in rows]
        known = await self.known_items(ids)
        for r in rows:
            if known.get(str(r["id"])) != (r["title"], r["content_short"]):
                yield self.request(f"{API}/articles/{r['id']}?extract=0", self.parse_detail, summary=r["content_short"])
        page = response.meta["page"]
        if rows and self.should_continue(ids, known, ts(min(r["display_time"] for r in rows)), page):
            yield self.list_request(data["next_cursor"], page + 1)

    async def parse_detail(self, response: Response):
        a = response.json()["data"]
        yield self.item(
            response,
            external_id=str(a["id"]),
            url=f"https://wallstreetcn.com/articles/{a['id']}",
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

    def list_request(self, cursor: str, page: int) -> Request:
        return self.request(f"{API}/lives?channel=global-channel&limit=50&cursor={cursor}", self.parse_list, page=page)

    async def parse_list(self, response: Response):
        data = response.json()["data"]
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
        if rows and self.should_continue(ids, known, ts(min(x["display_time"] for x in rows)), page):
            yield self.list_request(data["next_cursor"], page + 1)
