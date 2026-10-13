import warnings

from scrapy.exceptions import ScrapyDeprecationWarning

from alphapulse.core.config import get_settings
from alphapulse.core.redis import KEY_PREFIX

BOT_NAME = "alphapulse"
SPIDER_MODULES = ["alphapulse.crawler.spiders"]

# 采集的是无需登录的公开 JSON 接口；限速由 RedisRateLimitMiddleware 统一控制
ROBOTSTXT_OBEY = False
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DEFAULT_REQUEST_HEADERS = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9"}
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False
# 2.19 默认开启的远程控制 HTTP 服务用不上，关掉避免开放端口
REMOTE_CONTROL_ENABLED = False
LOG_LEVEL = "INFO"

CONCURRENT_REQUESTS = 4
DOWNLOAD_TIMEOUT = 15
RETRY_TIMES = 2
# 429/403 不重试，交给来源退避
RETRY_HTTP_CODES = [500, 502, 503, 504, 522, 524, 408]

DOWNLOADER_MIDDLEWARES = {
    "alphapulse.crawler.middlewares.RedisRateLimitMiddleware": 100,
    # process_response 按优先级从大到小调用：排在 HttpCompression(590) 之后拿到解压后的响应体，
    # 排在 Retry(550) 之前，被重试的每次响应也都会记录
    "alphapulse.crawler.middlewares.RawResponseMiddleware": 580,
}
ITEM_PIPELINES = {"alphapulse.crawler.pipelines.NewsPipeline": 300}

# Scrapy-Redis：请求队列与起始任务放 Redis（DB 14，统一 ap: 前缀）
SCHEDULER = "scrapy_redis.scheduler.Scheduler"
DUPEFILTER_CLASS = "scrapy_redis.dupefilter.RFPDupeFilter"
SCHEDULER_PERSIST = True
SCHEDULER_QUEUE_KEY = f"{KEY_PREFIX}%(spider)s:requests"
SCHEDULER_DUPEFILTER_KEY = f"{KEY_PREFIX}%(spider)s:dupefilter"
REDIS_START_URLS_KEY = f"{KEY_PREFIX}%(name)s:start_urls"
REDIS_URL = get_settings().redis_url

# scrapy-redis 0.9.1 仍按旧签名给 stats 传 spider 参数，每个请求都告警，会刷屏实时日志
warnings.filterwarnings("ignore", category=ScrapyDeprecationWarning, module="scrapy_redis")
