from alphapulse.models.jin10 import Jin10Connection, Jin10SyncDay
from alphapulse.core.db import Base
from alphapulse.models.ai import AiProvider, ChatMessage, ChatSession, ContextVersion, SkillNode, SkillVersion
from alphapulse.models.calendar import EconomicRelease, EconomicRevision, EconomicSeriesValue
from alphapulse.models.market import Kline, KlineSyncState, MarketSymbol
from alphapulse.models.news import CrawlLog, CrawlRun, NewsBookmark, NewsItem, NewsRevision, RawBody, RawResponse, Source

__all__ = [
    "Jin10Connection",
    "Jin10SyncDay",
    "AiProvider",
    "Base",
    "ChatMessage",
    "ChatSession",
    "ContextVersion",
    "CrawlLog",
    "CrawlRun",
    "EconomicRelease",
    "EconomicRevision",
    "EconomicSeriesValue",
    "Kline",
    "KlineSyncState",
    "MarketSymbol",
    "NewsItem",
    "NewsBookmark",
    "NewsRevision",
    "RawBody",
    "RawResponse",
    "SkillNode",
    "SkillVersion",
    "Source",
]

# Register additive news research tables with Alembic metadata.
from alphapulse.news_research import models as news_research_models  # noqa: F401,E402
