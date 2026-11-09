import asyncio

import httpx
from fastapi import FastAPI


def test_workspace_fallback_preserves_api_errors_and_assets(app_env, tmp_path):
    from alphapulse.api.frontend import WorkspaceFiles
    (tmp_path/'index.html').write_text('<html>workspace</html>')
    (tmp_path/'assets').mkdir()
    (tmp_path/'assets/app.js').write_text('ok')
    app = FastAPI()
    app.mount('/', WorkspaceFiles(directory=tmp_path, html=True))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
            assert (await c.get('/news')).text == '<html>workspace</html>'
            assert (await c.get('/api/removed')).status_code == 404
            assert (await c.get('/assets/missing.js')).status_code == 404
            assert 'immutable' in (await c.get('/assets/app.js')).headers['cache-control']
            assert (await c.get('/version.json')).headers['cache-control'] == 'no-cache'
    asyncio.run(check())
