"""Reviewed phrase equivalence, without modifying literal evidence."""

from copy import deepcopy
from sqlalchemy import select
from pydantic import Field, model_validator
from alphapulse.news_research.contracts import Strict
from alphapulse.news_research.models import VocabularyVersion


def key(value):
    return value.strip().casefold()


class Entry(Strict):
    canonical: str = Field(min_length=1, max_length=120)
    aliases: list[str]


class VocabularyInput(Strict):
    base_revision: int = Field(ge=0)
    entries: list[Entry]

    @model_validator(mode="after")
    def unambiguous(self):
        owners = {}
        for index, entry in enumerate(self.entries):
            entry.canonical = entry.canonical.strip()
            entry.aliases = [a.strip() for a in entry.aliases]
            local = set()
            for word in [entry.canonical, *entry.aliases]:
                normalized = key(word)
                if not normalized or len(word) > 120 or normalized in local:
                    raise ValueError("词条为空、过长或重复")
                local.add(normalized)
                if normalized in owners and owners[normalized] != index:
                    raise ValueError("规范词或别名属于多个词条")
                owners[normalized] = index
        return self


async def snapshot(session):
    row = await session.scalar(
        select(VocabularyVersion).order_by(VocabularyVersion.revision.desc()).limit(1)
    )
    return (
        {"revision": row.revision, "entries": row.entries}
        if row
        else {"revision": 0, "entries": []}
    )


def lookup(vocabulary):
    return {
        key(word): entry
        for entry in vocabulary["entries"]
        for word in [entry["canonical"], *entry["aliases"]]
    }


def groups(terms, vocabulary):
    mapping = lookup(vocabulary)
    result = {}
    for term in terms:
        normalized = key(term)
        if not normalized:
            continue
        entry = mapping.get(normalized)
        identity = key(entry["canonical"]) if entry else normalized
        result[identity] = (
            list(dict.fromkeys(key(w) for w in [entry["canonical"], *entry["aliases"]]))
            if entry
            else [normalized]
        )
    return list(result.values())


def expand(terms, vocabulary):
    return list(
        dict.fromkeys(word for group in groups(terms, vocabulary) for word in group)
    )


def normalize(keywords, vocabulary):
    mapping = lookup(vocabulary)
    result = {}
    for original in keywords:
        item = deepcopy(original)
        matches = {
            key(mapping[key(a)]["canonical"])
            for a in item["aliases"]
            if key(a) in mapping
        }
        if len(matches) > 1:
            raise ValueError("同一个TAG的原文别名指向不同主体")
        entry = next(
            (mapping[key(a)] for a in item["aliases"] if key(a) in mapping),
            mapping.get(key(item["canonical"])),
        )
        if entry:
            item["canonical"] = entry["canonical"]
        identity = key(item["canonical"])
        if identity in result:
            result[identity]["aliases"] = list(
                dict.fromkeys([*result[identity]["aliases"], *item["aliases"]])
            )
        else:
            result[identity] = item
    return list(result.values())
