#!/usr/bin/env python3
"""Resume the authorized 2025 news/calendar backfill. Default: read-only preview."""
import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from ci import locked, require, run, save
import deploy

NEWS = ('coindesk', 'bls', 'bea', 'ecb', 'eurostat', 'boe', 'ons', 'destatis', 'insee')
CALENDAR = ('bls', 'bea', 'fed', 'umich', 'dol', 'eia', 'adp', 'ecb', 'eurostat', 'boe', 'ons', 'destatis', 'insee')
BEGIN = '2025-01-01T00:00:00+08:00'


def python(code):
    return run('docker', 'exec', '-i', 'AlphaPulse-dev-api', 'python', '-', input=code)


def snapshot():
    return json.loads(python('''
import asyncio,json
from sqlalchemy import select,func
from alphapulse.core.db import SessionLocal,engine
from alphapulse.models import Source,NewsItem
from alphapulse.models.risk import RiskSettings
async def main():
 async with SessionLocal() as s:
  setting=await s.get(RiskSettings,1)
  rows=(await s.execute(select(NewsItem.source_code,func.count()).group_by(NewsItem.source_code))).all()
  print(json.dumps({'news_ai_enabled':bool(setting and setting.enabled),'news_counts':dict(rows)}))
 await engine.dispose()
asyncio.run(main())
'''))


def initialize():
    python('''
import asyncio,copy
from datetime import datetime
from sqlalchemy import select
from alphapulse.core.db import SessionLocal,engine
from alphapulse.core.us_news import SOURCES
async def main():
 async with SessionLocal() as s:
  from alphapulse.models import Source
  for source in await s.scalars(select(Source).where(Source.code.in_(SOURCES)).with_for_update()):
   target=datetime.fromisoformat('2025-01-01T00:00:00+08:00')
   if source.history_target==target: continue
   source.history_target=target;source.history_done=False
   cursor=copy.deepcopy(source.history_cursor or {})
   cursor.pop('history',None)
   if source.code=='sec': cursor['realtime']={'entry':0}
   source.history_cursor=cursor
  await s.commit()
 await engine.dispose()
asyncio.run(main())
''')


def attempt(task, state, directory, command):
    kind, source = task.split(':')
    if kind == 'news':
        name = f'{source}-{state["attempts"]}.jsonl.gz'
        target = '/tmp/alphapulse-' + name
        result = json.loads(run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-m',
            'alphapulse.crawler.history_capture', source, '--start', BEGIN, '--end', state['end'], '--output', target))
        run('docker', 'cp', 'AlphaPulse-dev-api:' + target, directory / name)
        run(*command, 'stop', 'crawler')
        try:
            run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-m', 'alphapulse.crawler.import_history',
                source, target, '--start', BEGIN)
        finally:
            run(*command, 'up', '-d', 'crawler')
        run('docker', 'exec', 'AlphaPulse-dev-api', 'rm', target)
        return result
    # Existing calendar backfill requires sole ownership while matching/creating releases.
    run(*command, 'stop', 'calendar')
    try:
        value = python('''
import asyncio,json
from datetime import date,datetime
from alphapulse.calendar.backfill import run
from alphapulse.core.db import engine
async def main():
 try:
  result=await run(date(2025,1,1),date.fromisoformat(END),commit=True,sources={SOURCE},until=datetime.fromisoformat(UNTIL))
  print(json.dumps(result,ensure_ascii=False))
 finally: await engine.dispose()
asyncio.run(main())
'''.replace('END', repr(datetime.fromisoformat(state['end']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat())).replace('SOURCE', repr(source)).replace('UNTIL', repr(state['end'])))
        return json.loads(value)
    finally:
        run(*command, 'up', '-d', 'calendar')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--once', action='store_true', help='One pass; leave cooldown tasks for a later resume')
    args = parser.parse_args()
    directory = deploy.INSTANCE / 'backfill-20250101'
    if not args.execute:
        print(json.dumps({'start': BEGIN, 'exclude': ['wscn_live', 'wscn_news'], 'baseline': snapshot(),
                          'state': str(directory / 'state.json')}, ensure_ascii=False))
        return
    directory.mkdir(exist_ok=True)
    with locked(directory / 'task.lock'):
        path = directory / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {'start': BEGIN,
            'end': datetime.now(UTC).isoformat(), 'baseline': snapshot(), 'initialized': False,
            'tasks': {f'{kind}:{source}': {'attempts': 0, 'status': 'pending', 'next_at': 0}
                      for kind, codes in [('news', NEWS), ('calendar', CALENDAR)] for source in codes}}
        require(not snapshot()['news_ai_enabled'], 'News AI must remain paused for collection backfill')
        save(path, state)
        while True:
            with locked(deploy.INSTANCE / '.deploy.lock'):
                require(not (deploy.INSTANCE / 'deployment.pending.json').exists(), 'Deployment in progress; resume later')
                release = json.loads((deploy.INSTANCE / 'deployment.json').read_text())
                command = deploy.compose(deploy.INSTANCE, release)
                if not state['initialized']:
                    run(*command, 'stop', 'crawler')
                    try:
                        initialize()
                        state['initialized'] = True
                        save(path, state)
                    finally:
                        run(*command, 'up', '-d', 'crawler')
                for name, task in state['tasks'].items():
                    if task['status'] == 'running':
                        task.update(status='retired' if task['attempts'] >= 3 else 'waiting',
                                    next_at=time.time() + 3600,
                                    errors=[{'error': '上次回补中断，保留已用额度并等待冷却'}])
                        save(path, state)
                    if task['status'] in ('done', 'retired') or task['next_at'] > time.time():
                        continue
                    task['attempts'] += 1
                    task['status'] = 'running'
                    save(path, state)
                    try:
                        result = attempt(name, {**task, 'end': state['end']}, directory, command)
                        save(directory / (name.replace(':', '-') + f'-{task["attempts"]}.json'), result)
                        errors = result.get('errors', []) + result.get('missing_announcements', [])
                        task['status'] = 'waiting' if errors else 'done'
                        task['errors'] = errors
                    except (RuntimeError, ValueError, OSError) as exc:
                        task.update(status='waiting', errors=[{'error': str(exc)[-4000:]}])
                    if task['status'] == 'waiting':
                        if task['attempts'] >= 3:
                            task['status'] = 'retired'
                        deadline = datetime.now(UTC) + timedelta(hours=1)
                        from email.utils import parsedate_to_datetime
                        for error in task['errors']:
                            value = error.get('retry_after')
                            if value:
                                try:
                                    at = datetime.now(UTC) + timedelta(seconds=int(value)) if value.isdigit() else parsedate_to_datetime(value)
                                    deadline = max(deadline, at)
                                except (ValueError, TypeError, OverflowError):
                                    pass
                        task['next_at'] = deadline.timestamp()
                    save(path, state)
                    print(name, task['status'], 'attempt', task['attempts'], flush=True)
            pending = [v for v in state['tasks'].values() if v['status'] not in ('done', 'retired')]
            if args.once or not pending:
                print(json.dumps({'tasks': {k: v['status'] for k, v in state['tasks'].items()}, 'snapshot': snapshot()}))
                return
            time.sleep(min(30, max(1, min(v['next_at'] for v in pending) - time.time())))


if __name__ == '__main__':
    main()
