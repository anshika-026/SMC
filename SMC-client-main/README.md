# SMC Client — Unique Footfall

Counts **distinct people** (not line crossings) on the two SMC camera feeds,
using the body-appearance Re-ID engine from MAINDV. Both cameras share one
identity gallery: someone seen on Camera 1 and again on Camera 2 is counted
once.

```
backend/    FastAPI service: RTSP readers + Re-ID unique footfall + /api/footfall/*
  app/reid/        Re-ID engine, vendored unchanged from MAINDV backend/app/reid
  app/footfall.py  counting service (ported from MAINDV footfall.py)
  app/camera_reader.py  one reconnecting RTSP reader per camera
  scripts/         model download, offline demo on a video, evaluation/replay tools
  tests/           unique-count spec (fake detector/embedder, no camera needed)
frontend/   React dashboard: Unique Footfall + Footfall UAT pages from MAINDV
docs/UNIQUE_FOOTFALL.md   engine design doc (thresholds, model choice, limits)
```

## Cameras

Only these two feeds are counted, configured in `backend/.env`:

| Slot | Stream |
|---|---|
| Camera 1 | `rtsp://144.79.198.149:8554/video/live?channel=1&subtype=0` |
| Camera 2 | `rtsp://144.79.198.149:8555/video/live?channel=1&subtype=0` |

Credentials go in `CAMERA_n_USERNAME` / `CAMERA_n_PASSWORD`, not in the URL.
They are percent-encoded automatically, so a password containing `@` works.
`backend/.env` is git-ignored.

## Run

Backend (Python 3.12 — the pins match MAINDV):

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate      # Linux: . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
pip install -r requirements.txt
cp .env.example .env                                  # then fill in camera passwords
python -m scripts.fetch_reid_model                    # Re-ID weights (strongly recommended)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

YOLOv8n weights (`yolov8n.pt`) are downloaded by ultralytics on first use.

Frontend:

```bash
cd frontend
cp .env.example .env
npm ci
npm run dev            # http://localhost:5180
```

For deployment, `npm run build` and restart the backend: it serves
`frontend/dist` at `/`, with the API on the same origin (`/api`).

## API

| Method | Path | |
|---|---|---|
| GET | `/api/health` | liveness + configured camera count |
| GET | `/api/footfall/summary` | unique today, new/returning, per-camera, hourly, visitors |
| GET | `/api/footfall/people` | every counted identity with snapshot ids |
| GET | `/api/footfall/snapshots/{id}` | snapshot JPEG |
| POST | `/api/footfall/reset` | UAT: restart the count from zero |
| GET | `/api/footfall/cameras/{id}/frame` | latest still, for drawing a zone |
| PUT | `/api/footfall/cameras/{id}/zone` | counting zone polygon (0..1 fractions) or `null` |

Set `API_TOKEN` in `backend/.env` (and the same value as `VITE_API_TOKEN`
in `frontend/.env`) to require a Bearer token.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -v
```
