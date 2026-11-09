import asyncio
import base64
import gzip
import json
from pathlib import Path
import runpy

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/capture_congress.py'


def test_capture_redacts_and_reuses_successful_response(app_env, tmp_path, monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    monkeypatch.setattr(module['time'], 'sleep', lambda seconds: None)
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'request': {'apiKey': 'private-test-secret'}, 'pagination': {'next': 'https://api.congress.gov/v3/bill?page=2&api_key=private-test-secret'}})
    path = tmp_path / 'cache.gz'
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        capture = module['Capture'](path, 'private-test-secret', client=client)
        url = 'https://api.congress.gov/v3/bill?format=json'
        first = capture.fetch(url)
        assert capture.fetch(url) == first and len(seen) == 1
        loaded = json.load(gzip.open(path, 'rt'))
        assert 'private-test-secret' not in base64.b64decode(loaded[url]['body']).decode()
        assert seen[0].headers['X-Api-Key'] == 'private-test-secret'
        capture.fetch('https://www.govinfo.gov/test.pdf')
        assert 'X-Api-Key' not in seen[-1].headers


def test_capture_stops_after_rate_limit_and_reports_incomplete(app_env, tmp_path):
    module = runpy.run_path(str(SCRIPT))
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '123'})
    path = tmp_path / 'cache.gz'
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        capture = module['Capture'](path, 'private-test-secret', client=client)
        report = asyncio.run(module['run'](capture))
    assert len(calls) == 1 and not path.exists()
    assert report['accepted'] is False
    assert len(report['cases']) == 1
    assert '123' in report['cases'][0]['error']
    assert 'private-test-secret' not in json.dumps(report)


def test_capture_keeps_progress_on_budget_exhaustion(app_env, tmp_path, monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    monkeypatch.setattr(module['time'], 'sleep', lambda seconds: None)
    path = tmp_path / 'cache.gz'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'body'))) as client:
        capture = module['Capture'](path, 'test', limit=1, client=client)
        capture.fetch('https://www.govinfo.gov/first.pdf')
        with pytest.raises(module['CaptureBlocked'], match='预算'):
            capture.fetch('https://www.govinfo.gov/second.pdf')
        resumed = module['Capture'](path, 'test', limit=1, client=client)
        assert resumed.fetch('https://www.govinfo.gov/first.pdf') == b'body'
        assert resumed.requests == 0
        assert resumed.fetch('https://www.govinfo.gov/second.pdf') == b'body'


def test_capture_replays_all_case_callbacks_without_external_io(app_env, tmp_path, monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    monkeypatch.setattr(module['time'], 'sleep', lambda seconds: None)
    def handle(request):
        path = request.url.path
        if request.url.host == 'www.congress.gov':
            return httpx.Response(200, text='<p>The law establishes regulatory requirements for stablecoins and digital assets.</p>')
        if path == '/v3/committee-meeting/119':
            data = {'committeeMeetings': [{'url': 'https://api.congress.gov/v3/committee-meeting/119/house/1'}]}
        elif '/committee-meeting/' in path:
            data = {'committeeMeeting': {'title': 'Digital asset regulation', 'updateDate': '2025-01-01T12:00:00Z'}}
        elif path.endswith('/text'):
            data = {'textVersions': [{'date': '2025-01-01', 'type': 'Introduced', 'formats': [{'type': 'Formatted Text', 'url': 'https://www.congress.gov' + path + '.html'}]}]}
        elif path.endswith('/actions'):
            data = {'actions': [{'actionDate': '2025-01-01', 'text': 'Passed'}]}
        else:
            data = {'bill': {'title': 'Digital asset regulation', 'updateDate': '2025-01-01'}}
        return httpx.Response(200, json=data)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        capture = module['Capture'](tmp_path / 'cache.gz', 'test', client=client)
        report = asyncio.run(module['run'](capture))
        assert report['accepted'] is True
        assert len(report['cases']) == 4
        assert sum(case['items'] for case in report['cases']) == 7
        resumed = module['Capture'](capture.path, 'test', client=client)
        assert asyncio.run(module['run'](resumed))['accepted'] is True
        assert resumed.requests == 0
