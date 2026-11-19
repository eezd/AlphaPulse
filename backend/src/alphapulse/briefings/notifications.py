"""运行异常发件箱。TG 结果不明时不自动重发；错误不保留含凭据 URL。"""
import asyncio
import hashlib
import logging
import re
import traceback
from datetime import UTC, datetime, timedelta
import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from alphapulse.core.crypto import decrypt
from alphapulse.core.db import SessionLocal
from alphapulse.briefings.config import settings_row
from alphapulse.briefings.models import Analysis, BriefingSettings, Notification, ReportPart

MAX_ATTEMPTS = 3


class TelegramURLFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += '\n' + ''.join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg = re.sub(r'/bot[^/\s]+', '/bot[REDACTED]', message)
        record.args = ()
        return True


# HTTPX 的 INFO 和 httpcore 的 DEBUG 可能记录带 token 的请求路径。
for logger_name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(logger_name).addFilter(TelegramURLFilter())


async def enqueue(session, incident, key, message):
    await session.execute(insert(Notification).values(incident=incident, dedup_key=key, message=message[:3500],
                          state='pending', attempts=0).on_conflict_do_nothing(index_elements=['dedup_key']))


async def enqueue_report_alert(session, report):
    settings = await settings_row(session)
    reason = (report.error or '资料不完整').split('；')[0][:180]
    incident = 'coverage' if report.input is None else 'report_generation'
    key = (f'backfill:{incident}:{settings.control_revision}' if report.lane == 'historical_backfill'
           else f'window:{report.kind}:{report.report_date}:{incident}')
    await enqueue(session, incident, key,
        f'AlphaPulse 报告运行异常\n报告 #{report.id} {report.report_date} {report.kind}\n'
        f'状态：{report.state}；原因：{reason}\n'
        f'覆盖：{report.coverage or {}}\n队列仍保留任务和审计，请在网页检查并补报。')


async def enqueue_upstream_alert(session, configuration, settings):
    provider_id, model = configuration.split(':', 1)
    completed = []
    for cls in (Analysis, ReportPart):
        value = await session.scalar(select(func.max(cls.finished_at)).where(cls.state == 'done',
               cls.rules['provider_id'].as_integer() == int(provider_id), cls.rules['model'].as_string() == model))
        if value:
            completed.append(value)
    epoch = max(completed).isoformat() if completed else f'revision:{settings.control_revision}'
    digest = hashlib.sha256((configuration + ':' + epoch).encode()).hexdigest()
    await enqueue(session, 'suspected_upstream', f'upstream:{digest}',
        f'AlphaPulse 疑似上游异常\n配置 {configuration} 的活跃请求全部在重试等待，连续10分钟未成功。\n'
        '这不是已确认的上游宕机；请查看 AI 队列。现有请求仍保留上下文并继续独立重试。')


async def recover(session):
    await session.execute(update(Notification).where(Notification.state == 'sending').values(
        state='unknown', error='进程中断，投递结果未知；请在 Telegram 核对，未自动重发'))


async def send_one(*, client_factory=httpx.AsyncClient):
    async with SessionLocal() as session:
        settings = await session.get(BriefingSettings, 1)
        if settings is None or not settings.enabled or not settings.telegram_enabled or not settings.telegram_token or not settings.telegram_chat_id:
            return False
        row = await session.scalar(select(Notification).where(Notification.state == 'pending',
             Notification.next_attempt_at <= datetime.now(UTC)).order_by(Notification.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return False
        token = decrypt(settings.telegram_token.encode())
        chat_id, message = settings.telegram_chat_id, row.message
        row.state, row.attempts = 'sending', row.attempts + 1
        identifier, attempts = row.id, row.attempts
        await session.commit()
    state, error, delay = 'unknown', None, None
    try:
        async with asyncio.timeout(8), client_factory(timeout=httpx.Timeout(5), follow_redirects=False) as client:
            response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                         json={'chat_id': chat_id, 'text': message})
        if response.status_code == 429:
            try:
                retry_after = int(response.json()['parameters']['retry_after'])
            except (ValueError, KeyError, TypeError):
                retry_after = 0
            if attempts < MAX_ATTEMPTS and 0 < retry_after <= 86400:
                state, delay, error = 'pending', retry_after, 'Telegram 限流，按 retry_after 等待'
            else:
                state, error = 'failed', 'Telegram 限流但重试条件不满足或次数已达上限'
        elif response.is_success:
            try:
                successful = response.json().get('ok') is True
            except ValueError:
                successful = False
            state, error = ('sent', None) if successful else ('unknown', 'Telegram 未确认消息成功，请人工核对')
        elif 400 <= response.status_code < 500:
            state, error = 'failed', f'Telegram 拒绝消息（HTTP {response.status_code}）'
        else:
            state, error = 'unknown', f'Telegram 响应不能确认投递（HTTP {response.status_code}），请人工核对'
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        if attempts < MAX_ATTEMPTS:
            state, delay, error = 'pending', 5 * attempts, '连接前失败，稍后有限重试'
        else:
            state, error = 'failed', '连接前失败已达重试上限'
    except asyncio.CancelledError:
        raise
    except Exception as exception:
        # 不保存 str(exception)：httpx 的异常中可能包含 /botTOKEN/ URL。
        state, error = 'unknown', f'投递结果未知（{type(exception).__name__}），请人工核对；未自动重发'
    async with SessionLocal() as session:
        row = await session.scalar(select(Notification).where(Notification.id == identifier).with_for_update())
        if row.state == 'sending':
            row.state, row.error = state, error
            if delay is not None:
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            if state == 'sent':
                row.sent_at = datetime.now(UTC)
            await session.commit()
    return True


async def sender(stopping):
    while not stopping.is_set():
        try:
            await send_one()
        except Exception as error:
            # 通知故障独立于模型任务；不输出可能带凭据的异常正文。
            logging.getLogger(__name__).error('异常通知发送器暂不可用（%s）', type(error).__name__)
        await asyncio.sleep(1)
