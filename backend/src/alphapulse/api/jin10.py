"""Administrator-only Jin10 connection; credentials never leave the backend."""
from datetime import UTC, datetime, timedelta
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.calendar import jin10_auth
from alphapulse.core.auth import AuthSession, check_session, require_admin
from alphapulse.core.crypto import decrypt, encrypt
from alphapulse.core.db import get_session
from alphapulse.models.jin10 import Jin10Connection

router = APIRouter(prefix="/api/settings/jin10", tags=["settings"], dependencies=[Depends(require_admin)])
Session = Annotated[AsyncSession, Depends(get_session)]
Principal = Annotated[AuthSession, Depends(require_admin)]


async def connection(session: AsyncSession, *, lock: bool = False) -> Jin10Connection:
    query = select(Jin10Connection).where(Jin10Connection.id == 1)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(query)
    if row is None:
        raise HTTPException(503, "金十连接尚未初始化，请先完成数据库迁移")
    return row


def clear_task(row: Jin10Connection) -> None:
    row.task_id = row.owner = row.qr_encrypted = row.expires_at = row.next_poll_at = None


def public_status(row: Jin10Connection) -> dict:
    return {"connected": row.token_encrypted is not None, "connected_at": row.connected_at,
            "error": row.error, "sync_state": "awaiting_validation" if row.token_encrypted else "disconnected"}


@router.get("")
async def status(session: Session) -> ApiResponse[dict]:
    return success(public_status(await connection(session)))


@router.post("/qr", status_code=201)
async def create_qr(session: Session, principal: Principal) -> ApiResponse[dict]:
    row = await connection(session, lock=True)
    try:
        token = await jin10_auth.generate_qr()
    except jin10_auth.Jin10Error as exc:
        raise HTTPException(502, str(exc)) from None
    await check_session(principal)
    row.task_id, row.owner = secrets.token_urlsafe(24), principal.jti
    row.qr_encrypted = encrypt(token)
    row.expires_at = datetime.now(UTC) + timedelta(seconds=60)
    row.next_poll_at = datetime.now(UTC) + timedelta(seconds=5)
    row.error = None
    await session.commit()
    return success({"task_id": row.task_id, "image": jin10_auth.qr_image(token), "expires_at": row.expires_at})


async def owned_task(session: AsyncSession, principal: AuthSession, task_id: str) -> Jin10Connection:
    row = await connection(session, lock=True)
    if row.task_id != task_id or row.owner != principal.jti:
        raise HTTPException(404, "扫码任务不存在或已失效")
    return row


@router.post("/qr/{task_id}/poll")
async def poll(task_id: str, session: Session, principal: Principal) -> ApiResponse[dict]:
    row = await owned_task(session, principal, task_id)
    now = datetime.now(UTC)
    if row.expires_at <= now:
        clear_task(row)
        await session.commit()
        return success({"state": "expired"})
    if row.next_poll_at > now:
        return success({"state": "pending"})
    token = decrypt(row.qr_encrypted)
    # Claim the remaining QR lifetime, then release the lock before network I/O.
    # A failed request can be retried with a new QR; overlapping polls cannot log in twice.
    row.next_poll_at = row.expires_at
    await session.commit()
    error = None
    try:
        state, credential = await jin10_auth.poll_qr(token)
    except jin10_auth.Jin10Error as exc:
        error = str(exc)
    # Cancellation, replacement and disconnection must invalidate both success and failure.
    row = await owned_task(session, principal, task_id)
    await check_session(principal)
    # Recheck expiry after network I/O, including user confirmation latency.
    if datetime.now(UTC) >= row.expires_at:
        state = "expired"
    elif error is not None:
        row.error = error
        clear_task(row)
        await session.commit()
        raise HTTPException(502, error)
    if state == "connected":
        row.token_encrypted = encrypt(credential)
        row.connected_at = datetime.now(UTC)
        row.error = None
    if state != "pending":
        clear_task(row)
    else:
        row.next_poll_at = datetime.now(UTC) + timedelta(seconds=5)
    await session.commit()
    return success({"state": state})


@router.delete("/qr/{task_id}")
async def cancel(task_id: str, session: Session, principal: Principal) -> ApiResponse[None]:
    row = await owned_task(session, principal, task_id)
    await check_session(principal)
    clear_task(row)
    await session.commit()
    return success(None)


@router.delete("")
async def disconnect(session: Session, principal: Principal) -> ApiResponse[None]:
    row = await connection(session, lock=True)
    await check_session(principal)
    clear_task(row)
    row.token_encrypted = row.connected_at = row.error = None
    await session.commit()
    return success(None)
