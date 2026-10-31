"""Read the same official/provider merge for history without rewriting either source."""
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.calendar.catalog import FAMILIES
from alphapulse.calendar.jin10_view import MAPPINGS, definition, merge_items
from alphapulse.calendar.service import indicator_history, release_rows
from alphapulse.models import EconomicRelease, EconomicRevision


def changed(initial: str | None, current: str) -> bool:
    if initial is None:
        return False
    try:
        return Decimal(initial) != Decimal(current)
    except InvalidOperation:
        return initial != current


async def calendar_history(session: AsyncSession, selected: EconomicRelease, indicator: str, limit: int) -> list[dict]:
    if selected.source == "jin10":
        match = definition(selected.values["_jin10"])
        if match is None:
            return await indicator_history(session, selected, indicator, limit)
        family, key, stage = match
    else:
        family, key, stage = selected.family, indicator, selected.stage
    # IDs and exact definitions are verified by the normal read merge as well.
    provider_keys = [f"jin10_{id_}" for mapping in MAPPINGS.values() for (id_, _), value in mapping.items()
                     if value == (family, key, stage)]
    if not provider_keys:
        return await indicator_history(session, selected, indicator, limit)
    candidates = list(await session.scalars(select(EconomicRelease).where(
        EconomicRelease.scheduled_at <= selected.scheduled_at,
        EconomicRelease.schedule_status != "withdrawn",
        or_((EconomicRelease.source == FAMILIES[family].source) & (EconomicRelease.family == family)
            & EconomicRelease.stage.is_not_distinct_from(stage),
            (EconomicRelease.source == "jin10") & EconomicRelease.values["_jin10"]["indicator"].as_string().in_(provider_keys)),
    ).order_by(EconomicRelease.scheduled_at.desc(), EconomicRelease.id.desc()).limit(limit * 2)))
    now = datetime.now(UTC)
    official = [(row, item) for row in candidates if row.source != "jin10" for item in release_rows(row, now) if item["indicator"] == key]
    extras = [row for row in candidates if row.source == "jin10" and definition(row.values["_jin10"]) == (family, key, stage)]
    items = [item for item in merge_items(official, extras, now) if item["actual"] is not None]
    items.sort(key=lambda r: (r["scheduled_at"], r["release_id"]), reverse=True)
    items = items[:limit]
    by_id = {row.id: row for row in candidates}
    revisions = list(await session.scalars(select(EconomicRevision).where(
        EconomicRevision.release_id.in_(by_id),
    ).order_by(EconomicRevision.release_id, EconomicRevision.revision_no)))
    first = {}
    for revision in revisions:
        owner = by_id[revision.release_id]
        value_key = owner.values["_jin10"]["indicator"] if owner.source == "jin10" else key
        actual = revision.payload["values"].get(value_key, {}).get("actual")
        if actual is not None:
            first.setdefault(owner.id, actual)
    output = []
    for item in items:
        owner = by_id[item["release_id"]]
        if item["field_sources"]["actual"]["source"] == "jin10" and owner.source != "jin10":
            owner = by_id[item["related_release_id"]]
        initial = first.get(owner.id)
        output.append({"release_id": item["release_id"], "scheduled_at": item["scheduled_at"], "title": item["title"],
                       "period": item["period"], "period_label": item["period_label"], "stage": item["stage"],
                       "actual": item["actual"], "previous": item["previous"], "forecast": item["forecast"],
                       "forecast_before_release": item["forecast_before_release"], "forecast_observed_at": item["forecast_observed_at"],
                       "first_recorded_actual": initial, "first_received_at": owner.first_received_at,
                       "release_url": item["field_sources"]["actual"]["url"], "field_sources": item["field_sources"],
                       "revised": changed(initial, item["actual"])})
    return output
