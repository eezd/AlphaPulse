"""Optional compiled workspace, served after all API routes."""
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles


class WorkspaceFiles(StaticFiles):
    async def get_response(self, path, scope):
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path.startswith('assets/'):
                raise
            response = FileResponse(Path(self.directory) / 'index.html')
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if path.startswith('assets/') else 'no-cache'
        return response
