"""Resolve manual Actions inputs once, without dispatching another workflow."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_URL = 'https://git.134333.xyz/api/v1'
OWNER = 'eezd'
REPO = 'AlphaPulse'
SHA = re.compile(r'[0-9a-f]{40}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normalize_inputs(inputs):
    require(isinstance(inputs, dict) and set(inputs) <= {'source_branch', 'source_sha'}, 'Unexpected release inputs')
    branch, requested = inputs.get('source_branch', ''), inputs.get('source_sha', '')
    require(isinstance(branch, str) and isinstance(requested, str), 'Release inputs must be strings')
    branch, requested = branch.strip(), requested.strip()
    require(branch and not branch.startswith('-'), 'Invalid source branch')
    valid = subprocess.run(['git', 'check-ref-format', 'refs/heads/'+branch], capture_output=True)
    require(valid.returncode == 0, 'Invalid source branch')
    require(not requested or SHA.fullmatch(requested), 'Expected optional full source SHA')
    return {'source_branch': branch, 'source_sha': requested}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(path, token):
    parsed = urllib.parse.urlsplit(API_URL)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and '@@' not in API_URL, 'Invalid Forgejo API configuration')
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(API_URL.rstrip('/')+path, headers={
        'Authorization': 'token '+token, 'Accept': 'application/json', 'User-Agent': 'git-init-actions-resolver'})
    try:
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise ValueError(f'Forgejo source lookup failed: HTTP {code}') from None
    except (OSError, urllib.error.URLError):
        raise ValueError('Forgejo source lookup failed: network error') from None


def resolve():
    require(os.environ.get('CONTROL_REF') == 'refs/heads/main', 'Control workflow must run from main')
    control = os.environ.get('CONTROL_SHA', '')
    run_id = os.environ.get('CI_RUN_ID', '')
    require(SHA.fullmatch(control) and run_id.isdecimal() and int(run_id) > 0, 'Invalid Actions identity')
    inputs = normalize_inputs({'source_branch': os.environ.get('SOURCE_BRANCH', ''),
                               'source_sha': os.environ.get('SOURCE_SHA', '')})
    token = os.environ.get('FORGEJO_RELEASE_TOKEN')
    require(token, 'Missing Actions Secret RELEASE_TOKEN / FORGEJO_RELEASE_TOKEN')
    base = '/repos/'+urllib.parse.quote(OWNER, safe='')+'/'+urllib.parse.quote(REPO, safe='')
    branch = request(base+'/branches/'+urllib.parse.quote(inputs['source_branch'], safe=''), token)
    head = branch['commit']['id']
    require(isinstance(head, str) and SHA.fullmatch(head), 'Invalid source branch HEAD')
    sha = inputs['source_sha'] or head
    commit = request(base+'/git/commits/'+sha, token)
    date = datetime.datetime.fromisoformat(commit['commit']['committer']['date'].replace('Z', '+00:00'))
    require(date.tzinfo is not None, 'Source commit date requires timezone')
    version = date.astimezone(datetime.timezone.utc).strftime('%Y%m%d')+'-'+sha[:8]
    return {'schema': 1, 'source_branch': inputs['source_branch'], 'source_sha': sha, 'source_branch_head': head,
            'release_inputs': inputs, 'version': version, 'control_sha': control,
            'release_run_id': int(run_id), 'release_request_id': str(uuid.uuid4())}


def main():
    destination = Path(os.environ['RELEASE_CONTEXT'])
    output = Path(os.environ['GITHUB_OUTPUT'])
    context = resolve()
    destination.write_text(json.dumps(context, indent=2)+'\n')
    with output.open('a') as stream:
        for key, value in (('source_sha', context['source_sha']), ('version', context['version']),
                           ('request_id', context['release_request_id'])):
            stream.write(key+'='+value+'\n')
    print(json.dumps(context, indent=2), flush=True)


if __name__ == '__main__':
    main()
