"""
main.py

SMC unique-footfall API. Run from backend/:

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Serves /api/footfall/* (see footfall_routes.py) and, when frontend/dist
exists (npm run build), the dashboard itself at /.
"""

import contextlib
import logging
from pathlib import Path

# Imported on the main thread before any camera thread starts: ultralytics
# and torchreid both import torchvision on first use, and two threads doing
# that at once fail with a partially-initialised-module error.
import torchvision.transforms  # noqa: F401
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, footfall, footfall_routes, lifecycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    footfall.service.start()
    yield
    lifecycle.begin_shutdown()


app = FastAPI(title="SMC Unique Footfall", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
app.include_router(footfall_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "cameras": len(config.cameras())}


_DIST = (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        """Real files from dist (logo, favicon); any other non-API path gets
        index.html so a refresh or deep link to /footfall-uat works."""
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        target = (_DIST / path).resolve()
        if path and target.is_file() and target.is_relative_to(_DIST):
            return FileResponse(target)
        return FileResponse(_DIST / "index.html")
