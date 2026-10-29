# SMC Client

## Ports

SMC uses its own ports so it never clashes with the other projects on this
machine: MAINDV's backend uses **8821** and deco-vision's dev server uses **5173**.

| Service | Port | Address | Set in |
|---|---|---|---|
| Backend (API + live video) | **8822** | http://127.0.0.1:8822 | `backend/.env` → `PORT=8822` (Docker: `docker-compose.yml`) |
| Frontend (web app) | **5180** | http://localhost:5180 | `frontend/vite.config.js` (`port: 5180`, fails rather than switching port) |
| RTMP ingest (cameras pushing video) | **1935** | rtmp://&lt;this PC&gt;:1935/live | `SMC/.env` → `RTMP_PUBLIC_PORT` (Docker only) |

The frontend finds the backend through `frontend/.env` →
`VITE_API_BASE_URL=http://127.0.0.1:8822/api`, and the backend only accepts the
frontend's address listed in `CORS_ORIGINS` (`http://localhost:5180`). If you ever
change a port, change it in both places, then restart both.

## Folder layout

| Path | What it is |
|---|---|
| `backend/` | Python API, camera readers and all AI (face recognition, footfall, staff count, visitors). Its own Python environment is `backend/.venv`. |
| `frontend/` | React web app (Vite). |
| `docs/` | Design notes (e.g. `UNIQUE_FOOTFALL.md`). |
| `mediamtx/` | Config of the RTMP ingest server (cameras that push video), used by Docker. |
| `scripts/start-smc.bat` | Starts backend + frontend natively in two windows. |
| `scripts/docker-seed-data.ps1` | One time: copies `backend/data` into the Docker data volume. |
| `scripts/docker-ingest-env.ps1` | One time: creates `SMC/.env` with the RTMP ingest secrets. |
| `docker-compose.yml` | Runs the backend and the RTMP ingest server in Docker. |
| `.env` (not in git) | RTMP ingest settings and secrets for Docker. `backend/.env` holds the backend's own settings. |

## Running SMC

SMC has two parts: the **backend** (API, cameras, all AI) and the **frontend**
(the web app). On this PC the backend runs in **Docker**, because Windows Smart
App Control blocks PyTorch's native files when Python runs directly on Windows.
The frontend runs normally with Node.

### 1. Backend (Docker) - the way it runs on this PC

Requires Docker Desktop (with WSL 2) to be **running**: open Docker Desktop and
wait until it shows the engine is running before using any `docker` command.

```powershell
cd C:\Users\lenovo\Desktop\SMC

# FIRST-EVER SETUP ONLY (already done on this PC - skip these two)
.\scripts\docker-seed-data.ps1      # copies backend\data into Docker's data volume;
                                    # refuses if the volume already has data (it would overwrite live data)
.\scripts\docker-ingest-env.ps1     # creates SMC\.env (RTMP push settings + secrets); keeps an existing one

# start / update (rebuilds after code changes)
docker compose up -d --build

# check it is running - wait for "Uvicorn running on http://0.0.0.0:8822"
docker compose logs -f backend

# stop
docker compose stop
```

The backend is then at http://127.0.0.1:8822. Allow 1-2 minutes after a start
for the face models to load on every camera.

Create the first admin login (one time):

```powershell
docker compose exec backend python -m app.manage create-admin --email you@example.com
```

### 2. Frontend

```powershell
cd C:\Users\lenovo\Desktop\SMC\frontend
Copy-Item .env.example .env         # first time only; Linux/macOS: cp .env.example .env
npm ci                              # first time, or after package.json changes
npm run dev                         # http://localhost:5180
```

Open http://localhost:5180 and log in.

### Backend without Docker (Linux, or a Windows PC that does not block PyTorch)

Use **one** Python environment: `backend\.venv`. Do not create another one in
the `SMC` folder.

```powershell
cd C:\Users\lenovo\Desktop\SMC\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Linux/macOS: . .venv/bin/activate
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
python -m pip install -r requirements.txt
Copy-Item .env.example .env         # then set JWT_SECRET (see the comments in .env)
python -m app.manage create-admin --email you@example.com
python -m scripts.fetch_reid_model
python -m app.serve                 # http://127.0.0.1:8822
```

Or start backend and frontend together: `.\scripts\start-smc.bat`.

**VS Code:** Ctrl+Shift+P → *Python: Select Interpreter* →
`SMC\backend\.venv\Scripts\python.exe`. Otherwise VS Code may create a second,
unneeded environment in the `SMC` folder.

## Configuration

| File | Holds |
|---|---|
| `backend/.env` | Backend settings: server and port, security, enabled features, camera and face-recognition tuning. Each option is explained in the file. Template: `backend/.env.example`. |
| `SMC/.env` | Docker-only RTMP push settings and their secrets. Created by `scripts/docker-ingest-env.ps1`. |
| `frontend/.env` | `VITE_API_BASE_URL` (the backend's address) and `VITE_ENABLED_FEATURES`. |

Never commit any `.env` file (they are git-ignored); the `*.env.example`
templates are committed.

## Cameras

Cameras are added in **Camera Management** (site **Noida Site**). The current
cameras are pulled over RTSP from the site's public address:

| Name | Host | Port | Stream path | Analytics |
|---|---|---|---|---|
| CAM1 | 144.79.198.149 | 8554 | `/video/live?channel=1&subtype=0` | off (live view only) |
| CAM2 | 144.79.198.149 | 8555 | `/video/live?channel=1&subtype=0` | on |
| Cam3 | 144.79.198.149 | 8556 | `/video/live?channel=1&subtype=0` | on (Entry/Exit: also footfall) |
| CAM4 | 144.79.198.149 | 8557 | `/video/live?channel=1&subtype=0` | on |

User `admin`; the password is shared separately (never commit it). Unique
footfall counts on **Entry/Exit** cameras - draw each one's doorway zone on
**Footfall UAT**. A new camera at the site needs its own port forwarded on the
site's router (e.g. 8558) before it can be added the same way.

**RTMP push** (the camera sends video to SMC) only works if the camera can
reach the address in `SMC/.env` → `RTMP_PUBLIC_HOST`. Cameras on another
network (e.g. the wired office LAN while this PC is on a separate Wi-Fi router)
cannot reach it - use RTSP for those, or put this PC on the cameras' network.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker ...` says it cannot connect to the Docker API | Docker Desktop is not running - start it and wait for the engine. |
| Page shows no cameras / "check the backend" | Backend not up yet (models load for 1-2 min after a start) or stopped: `docker compose logs -f backend`. |
| `'vite' is not recognized` on `npm run dev` | `npm rebuild` in `frontend`, then `npm run dev` again. |
| `DLL load failed ... Application Control policy has blocked this file` | Windows blocks PyTorch outside Docker - run the backend with Docker. |
| Port already in use | Another project is on that port; see **Ports** above. |
| Live tile says "No video from the camera yet" | The camera is not sending video (RTMP push) or not reachable (RTSP). |

## Tests

```powershell
cd C:\Users\lenovo\Desktop\SMC\backend
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt     # first time only
python -m pytest tests -q
```

If Windows blocks PyTorch here as well (`DLL load failed ... Application Control
policy`), the tests that load it will fail on this PC; the rest still run.

`docs/UNIQUE_FOOTFALL.md` explains the Re-ID engine behind unique footfall.
