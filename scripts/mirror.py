#!/usr/bin/env python3
"""Reconcile all business Git references from Gitea to GitHub under one lock."""

import argparse
import os
from pathlib import Path
import shlex
import tempfile
import time

from ci import SOURCE, locked, request_json, require, run, save

TARGET = 'git@github.com:eezd/AlphaPulse.git'
DIRECTORY = Path('/docker/gitea/mirrors/AlphaPulse')


def refs(repository, remote=None, env=None):
    args = ['ls-remote', remote, 'refs/heads/*', 'refs/tags/*'] if remote else [
        'for-each-ref', '--format=%(objectname)\t%(refname)', 'refs/heads/', 'refs/tags/']
    output = run('git', '-C', repository, *args, env=env)
    return {ref: sha for line in output.splitlines() for sha, ref in [line.split()] if not ref.endswith('^{}')}


def synchronize(directory, source, target, env=None):
    directory = Path(directory)
    with locked(directory / '.mirror.lock'):
        repository = directory / 'repository.git'
        if not repository.exists():
            run('git', 'init', '--bare', repository)
        for _ in range(3):
            run('git', '-C', repository, 'fetch', '--prune', '--no-tags', source,
                '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*', env=env)
            snapshot = refs(repository)
            require('refs/heads/main' in snapshot, 'Source main is missing; refusing destructive synchronization')
            if run('git', '-C', repository, 'log', '--all', '--format=', '--name-only', '--', '.gitattributes', '**/.gitattributes'):
                run('git', '-C', repository, 'lfs', 'fetch', '--all', source, env=env)
                run('git', '-C', repository, 'lfs', 'push', '--all', target, env=env)
            run('git', '-C', repository, 'push', '--atomic', '--prune', target,
                '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*', env=env)
            require(refs(repository, target, env) == snapshot, 'GitHub references differ from the selected source snapshot')
            if refs(repository, source, env) == snapshot:
                save(directory / 'last-sync.json', {'refs': snapshot, 'verified_at': time.time()})
                return
        raise RuntimeError('Source kept changing; retry the mirror')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    # A write-enabled deploy key limits GitHub access to this repository.
    with tempfile.TemporaryDirectory(prefix='ap-mirror-auth-') as temporary:
        key = Path(temporary) / 'key'
        key.write_text(os.environ['ALPHAPULSE_GITHUB_SSH_KEY'])
        key.chmod(0o600)
        hosts = Path(temporary) / 'known_hosts'
        hosts.write_text(''.join('github.com ' + entry + '\n' for entry in request_json('https://api.github.com/meta')['ssh_keys']))
        command = ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'UserKnownHostsFile=' + str(hosts)]
        env = {**os.environ, 'GIT_SSH_COMMAND': shlex.join(command), 'GIT_TERMINAL_PROMPT': '0'}
        synchronize(DIRECTORY, SOURCE, TARGET, env)
    print('GitHub business references and history verified')


if __name__ == '__main__':
    main()
