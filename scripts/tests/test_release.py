"""Release baseline, remote-only dispatch and client failure contracts."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
spec=importlib.util.spec_from_file_location('manual',Path(__file__).resolve().parents[1]/'manual-release.py')
manual=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)

class SelectionTests(unittest.TestCase):
    def test_first_release_notes_are_exact_init(self):
        self.assertEqual(manual.release_notes(None,'a'*40),'init')
    def test_other_branch_is_not_used_as_baseline(self):
        r={'complete':True,'source_branch':'main','source_sha':'a','completed_at':1,'version':'v'}
        self.assertIsNone(manual.select_baseline([r],'dev','b'))
    def test_rewritten_branch_cannot_look_like_first_release(self):
        r={'complete':True,'source_branch':'dev','source_sha':'a','completed_at':1,'version':'v'}
        with patch.object(manual,'ancestor',return_value=False):
            with self.assertRaisesRegex(ValueError,'baseline'):manual.select_baseline([r],'dev','b')
    def test_closest_ancestor_wins(self):
        records=[{'complete':True,'source_branch':'dev','source_sha':c,'completed_at':1,'version':c} for c in ('a','b')]
        with patch.object(manual,'ancestor',return_value=True),patch.object(manual,'run',side_effect=['5','1']):
            self.assertEqual(manual.select_baseline(records,'dev','c')['source_sha'],'b')


class ClientTests(unittest.TestCase):
    def test_release_outside_repo_requires_explicit_branch(self):
        client=Path(__file__).resolve().parents[2]/'release.sh'
        with tempfile.TemporaryDirectory() as d:
            result=subprocess.run(['bash',str(client)],cwd=d,capture_output=True)
            self.assertEqual(result.returncode,2)
    def test_release_outside_repo_uses_no_local_git(self):
        import os
        client=Path(__file__).resolve().parents[2]/'release.sh'
        with tempfile.TemporaryDirectory() as d:
            env=dict(os.environ);env.pop('GITEA_TOKEN',None)
            result=subprocess.run(['bash',str(client),'feature/example'],cwd=d,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertIn('GITEA_TOKEN',result.stderr)

class RemoteClientTests(unittest.TestCase):
    def invoke_client(self, *, bad_sha=False, draft=False, dispatch_error=False):
        import contextlib,io,json,os,urllib.error,urllib.request
        client=(Path(__file__).resolve().parents[2]/'release.sh').read_text().split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        sha='a'*40;version='20261109-'+sha[:8];calls=[]
        class Response:
            def __init__(self,value):self.value=value
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):return json.dumps(self.value).encode()
        def open_request(request,**kw):
            url=request.full_url;calls.append(request)
            if '/branches/' in url:return Response({'commit':{'id':sha}})
            if '/git/commits/' in url:return Response({'commit':{'committer':{'date':'2026-11-09T10:00:00Z'}}})
            if '/dispatches?' in url:
                if dispatch_error:raise urllib.error.URLError('uncertain post')
                return Response({'workflow_run_id':123})
            if '/actions/runs/' in url:return Response({'status':'completed','conclusion':'success'})
            if '/releases/tags/' in url:return Response({'tag_name':version,'draft':draft,'prerelease':False,'assets':[{'name':'build.json','browser_download_url':'https://git.134333.xyz/download/build.json'}]})
            if '/download/build.json' in url:return Response({'source_sha':'b'*40 if bad_sha else sha,'version':version})
            raise AssertionError(url)
        from unittest.mock import Mock
        opener=Mock();opener.open.side_effect=open_request
        output=io.StringIO();code=0
        with patch.object(sys,'argv',['release.sh','feature/demo']),patch.dict(os.environ,{'GITEA_TOKEN':'fixture-only'}),patch.object(urllib.request,'build_opener',return_value=opener),contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            try:exec(compile(client,'release-client','exec'),{})
            except SystemExit as error:code=error.code
        return code,calls,output.getvalue()
    def test_dispatch_pins_sha_and_trusted_main(self):
        import json
        code,calls,output=self.invoke_client()
        self.assertEqual(code,0,output)
        posts=[r for r in calls if r.data]
        self.assertEqual(len(posts),1)
        body=json.loads(posts[0].data)
        self.assertEqual(body['ref'],'refs/heads/main')
        self.assertEqual(body['inputs']['source_branch'],'feature/demo')
        self.assertEqual(body['inputs']['source_sha'],'a'*40)
    def test_draft_release_cannot_succeed(self):self.assertEqual(self.invoke_client(draft=True)[0],1)
    def test_wrong_metadata_sha_cannot_succeed(self):self.assertEqual(self.invoke_client(bad_sha=True)[0],1)
    def test_uncertain_dispatch_is_never_reposted(self):
        code,calls,_=self.invoke_client(dispatch_error=True)
        self.assertEqual(code,1)
        self.assertEqual(sum(bool(r.data) for r in calls),1)
