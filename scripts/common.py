#!/usr/bin/env python3
"""Shared safe Git mirror helpers."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'git.134333.xyz'
REPOSITORY = 'eezd/AlphaPulse'
SOURCE = f'https://{REGISTRY}/{REPOSITORY}.git'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, input=None, env=None):
    result = subprocess.run(list(map(str, args)), input=input, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Callers never pass credentials as command arguments.
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-6000:]}{result.stdout[-6000:]}')
    return result.stdout.strip()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def locked(path):
    with Path(path).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def request_json(url, token=None, data=None, method=None, response_headers=None):
    headers = {'User-Agent': 'AlphaPulse-CI', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'token ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        if response_headers is not None:
            response_headers.update(response.headers)
        return json.load(response)
