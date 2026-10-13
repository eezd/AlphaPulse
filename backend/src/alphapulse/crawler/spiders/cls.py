import hashlib
import html
import json
import re
from urllib.parse import urlencode

from scrapy import Request
from scrapy.http import Response

from alphapulse.crawler.spiders.base import NewsSpider, ts


def signed_url(path: str, params: dict[str, str]) -> str:
    # 财联社网页前端的公开签名算法：md5(sha1(按键排序的查询串))
    query = {"app": "CailianpressWeb", "os": "web", "sv": "8.4.6", **params}
    query["sign"] = hashlib.md5(hashlib.sha1(urlencode(sorted(query.items())).encode()).hexdigest().encode()).hexdigest()
    return f"https://www.cls.cn{path}?{urlencode(query)}"


class ClsTelegraphSpider(NewsSpider):
    """财联社·电报：列表即全文；翻页需 refresh_type=1"""

    name = "cls_telegraph"
    request_headers = {"Referer": "https://www.cls.cn/telegraph"}

    def first_requests(self) -> list[Request]:
        return [self.request(signed_url("/v1/roll/get_roll_list", {"category": "", "last_time": "", "rn": "20"}), self.parse_list, page=1)]

    async def parse_list(self, response: Response):
        body = response.json()
        if body["errno"] != 0:
            raise ValueError(f"接口返回错误：{body}")
        rows = [x for x in body["data"]["roll_data"] if not x["is_ad"]]
        # 先查已入库集合，再产出条目：否则管道可能先落库，导致误判为没有新条目而停止翻页
        ids = [str(x["id"]) for x in rows]
        known = await self.known_items(ids)
        for x in rows:
            yield self.item(
                response,
                external_id=str(x["id"]),
                url=f"https://www.cls.cn/detail/{x['id']}",
                title=x["title"],
                summary=x["brief"],
                content_html=f"<p>{html.escape(x['content'])}</p>",
                published_at=ts(x["ctime"]),
                source_updated_at=ts(x["modified_time"]) if x.get("modified_time") else None,
                author=x.get("author"),
                tags=[s["subject_name"] for s in x.get("subjects") or []],
                extra={"level": x.get("level"), "stock_list": x.get("stock_list")},
            )
        page = response.meta["page"]
        if rows and self.should_continue(ids, known, ts(min(x["ctime"] for x in rows)), page):
            last_time = str(min(x["ctime"] for x in rows))
            params = {"category": "", "last_time": last_time, "refresh_type": "1", "rn": "20"}
            yield self.request(signed_url("/v1/roll/get_roll_list", params), self.parse_list, page=page + 1)


class ClsDepthSpider(NewsSpider):
    """财联社·深度（头条）：列表只有摘要，正文从详情页 __NEXT_DATA__ 解析"""

    name = "cls_depth"
    request_headers = {"Referer": "https://www.cls.cn/depth?id=1000"}

    def first_requests(self) -> list[Request]:
        # 首页不带 last_time 时 rn 无效，返回约 30 条且不按时间排序
        return [self.request(signed_url("/v3/depth/list/1000", {}), self.parse_list, page=1)]

    async def parse_list(self, response: Response):
        body = response.json()
        if body["errno"] != 0:
            raise ValueError(f"接口返回错误：{body}")
        rows = [x for x in body["data"] if not x["is_ad"]]
        ids = [str(x["id"]) for x in rows]
        known = await self.known_items(ids)
        for x in rows:
            if known.get(str(x["id"])) != (x["title"], x["brief"]):
                # 详情页不一定有 brief，摘要以列表为准（也用于下次比较是否变化）
                yield self.request(f"https://www.cls.cn/detail/{x['id']}", self.parse_detail, summary=x["brief"])
        page = response.meta["page"]
        if rows and self.should_continue(ids, known, ts(min(x["ctime"] for x in rows)), page):
            params = {"last_time": str(min(x["ctime"] for x in rows)), "rn": "20"}
            yield self.request(signed_url("/v3/depth/list/1000", params), self.parse_list, page=page + 1)

    async def parse_detail(self, response: Response):
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.S)
        if match is None:
            raise ValueError("详情页缺少 __NEXT_DATA__，页面结构可能已变化")
        a = json.loads(match.group(1))["props"]["pageProps"]["articleDetail"]
        yield self.item(
            response,
            external_id=str(a["id"]),
            url=f"https://www.cls.cn/detail/{a['id']}",
            title=a["title"],
            summary=response.meta["summary"],
            content_html=a["content"],
            published_at=ts(a["ctime"]),
            author=(a.get("author") or {}).get("name"),
            tags=[s["name"] for s in a.get("subject") or []],
            # 付费内容只保存公开部分
            is_paywalled=not a["isFree"],
            extra={"level": a.get("level")},
        )
