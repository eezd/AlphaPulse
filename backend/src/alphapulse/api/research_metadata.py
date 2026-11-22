"""公开文章出处投影，旧新闻没有此元数据。"""
from typing import Literal

from pydantic import BaseModel

ContentStatus = Literal['full', 'excerpt', 'metadata', 'extraction_failed']


class ResearchByline(BaseModel):
    name: str
    profile_url: str | None
    organization: str | None


class ResearchAttachment(BaseModel):
    url: str
    title: str | None
    status: Literal['link_only']


class ResearchArticleMetadata(BaseModel):
    publisher_code: str
    publisher_name: str
    bylines: list[ResearchByline]
    canonical_url: str
    original_url: str | None
    language: str | None
    section: str | None
    content_status: ContentStatus
    access_status: Literal['public', 'paywall', 'forbidden', 'unknown']
    attachments: list[ResearchAttachment]
