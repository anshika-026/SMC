# SMC (Deco Vision) - what you need to run it

Everything a new machine needs to run this repository, in order. The commands are
for **Windows PowerShell** (the project's main setup); Linux/macOS notes are given
where they differ.

## 1. Software to install

| Software | Version | Why | Get it |
|---|---|---|---|
| **Git** | any recent | clone the repository | https://git-scm.com/downloads |
| **Docker Desktop** (with WSL 2 on Windows) | 4.x or newer | runs the backend (Python, AI models) and the RTMP ingest server | https://www.docker.com/products/docker-desktop |
| **Node.js** (includes npm) | **20.19+ or 22.12+** | runs the web app (React + Vite 8) | https://nodejs.org |
| Python | 3.12 | **only** if you run the backend without Docker (section 7) | https://www.python.org/downloads |

Hardware: a 64-bit PC with **8 GB RAM minimum (16 GB recommended)** and about
**15 GB free disk** (Docker image ~6 GB, AI models ~0.5 GB, plus recordings). No GPU
is needed; everything runs on the CPU.

Everything the code itself depends on is pinned in the repository and installed
automatically by the steps below; you do not install these by hand:

| Part | Dependency list | Installed by |
|---|---|---|
| Backend (Python) | `backend/requirements.txt` (+ CPU PyTorch 2.13.0 / torchvision 0.28.0) | `docker compose up --build` (or `pip`, section 7) |
| Frontend (JavaScript) | `frontend/package.json` + `package-lock.json` | `npm ci` |
| RTMP ingest server | Docker image `bluenviron/mediamtx:1.9.3` | `docker compose up` |

## 2. Get the code

```powershell
git clone https://github.com/anshika-026/SMC..git SMC
cd SMC
```

Start **Docker Desktop** and wait until it says the engine is running.

## 3. Files that are NOT in the repository (you must create them)

The repository never contains secrets, the database, or the AI model weights.
Create these once:

### 3a. Backend settings - `backend/.env`

```powershell
Copy-Item backend\.env.example backend\.env
```

Open `backend\.env` and set at least:

| Setting | Value |
|---|---|
| `JWT_SECRET` | a random string of **at least 32 characters**, e.g. the output of `python -c "import secrets; print(secrets.token_hex(32))"` or any password generator |
| `ENABLED_FEATURES` | `footfall,face_recognition,staff_count,live_overlay,visitors` |
| `CORS_ORIGINS` | `http://localhost:5180,http://127.0.0.1:5180` (already the default) |

Every other option is explained in the file and can stay as it is.

### 3b. Docker ingest secrets - `SMC/.env`

```powershell
.\scripts\docker-ingest-env.ps1
```

Creates `SMC\.env` with random secrets for the RTMP ingest server (only needed
for cameras that *push* video; the backend will not start without it).
Linux/macOS: create `.env` next to `docker-compose.yml` by hand with
`INGEST_READ_PASSWORD=<random>`, `INGEST_HOOK_SECRET=<random>` and
`RTMP_PUBLIC_HOST=<this machine's LAN IP>`.

### 3c. The data volume

The database lives in a Docker volume named `smc_data`. On a new machine create
it empty:

```powershell
docker volume create smc_data
```

(`scripts\docker-seed-data.ps1` is only for copying an existing `backend\data`
folder into the volume. A fresh machine has none, so skip it.)

### 3d. AI model weights

The backend needs these model files (about 500 MB in total):

| File | Goes in | Used for |
|---|---|---|
| `yolov8n.pt` | `backend\models\` | person detection, footfall, staff count |
| `yolov8s.pt` | `backend\models\` | extra person detection pass |
| `yolov8n-face.pt` | `backend\models\` | face detection |
| `osnet_x0_25_msmt17.pth` | `backend\models\` | footfall (person re-identification) |
| `buffalo_l` (folder of `.onnx` files) | `%USERPROFILE%\.insightface\models\buffalo_l\` | face recognition |

Build the backend image once, then let it download the models into those folders:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.insightface" | Out-Null
docker compose build backend
docker run --rm -v "${PWD}\backend\models:/app/models" -v "$env:USERPROFILE\.insightface:/insightface" `
  -e MODEL_DIR=/app/models -e INSIGHTFACE_ROOT=/insightface smc-backend python -m scripts.fetch_models --skip-optional
```

`yolov8n-face.pt` cannot be downloaded automatically. Download it from
https://github.com/akanametov/yolo-face/releases and put it in `backend\models\`.
Then check that everything is in place (it must end with
**"All required models present."**):

```powershell
docker run --rm -v "${PWD}\backend\models:/app/models" -v "$env:USERPROFILE\.insightface:/insightface" `
  -e MODEL_DIR=/app/models -e INSIGHTFACE_ROOT=/insightface smc-backend python -m scripts.fetch_models --check --skip-optional
```

Linux/macOS: use `$HOME/.insightface` instead of `$env:USERPROFILE\.insightface`,
and also set `USERPROFILE=$HOME` before running `docker compose`, because the
compose file mounts `${USERPROFILE}/.insightface`.

### 3e. Frontend settings - `frontend/.env`

```powershell
Copy-Item frontend\.env.example frontend\.env
```

Then set this line in `frontend\.env` so that every module shows:

```
VITE_ENABLED_FEATURES=footfall,face_recognition,staff_count,live_overlay,visitors
```

## 4. Start the backend

```powershell
docker compose up -d --build
docker compose logs -f backend      # wait for "Uvicorn running on http://0.0.0.0:8822", then Ctrl+C
```

The first build takes 10-20 minutes (it installs PyTorch and the other Python
packages). After a start, allow 1-2 minutes for the face models to load.

Create your admin login (once; it asks for a password):

```powershell
docker compose exec backend python -m app.manage create-admin --email you@example.com
```

## 5. Start the web app

```powershell
cd frontend
npm ci          # first time, and after package.json changes
npm run dev     # http://localhost:5180
```

Open **http://localhost:5180** and log in with the admin account from step 4.
Add cameras under **Management → Camera**.

## 6. Ports used

| Service | Address |
|---|---|
| Web app | http://localhost:5180 |
| Backend API | http://127.0.0.1:8822 |
| RTMP ingest (cameras pushing video) | port 1935 |

These ports must be free. To change one, see the **Ports** section of `README.md`.

## 7. Backend without Docker (optional)

Only for Linux, or a Windows PC whose security policy (Smart App Control) does
not block PyTorch. Needs Python 3.12:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1                  # Linux/macOS: . .venv/bin/activate
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
python -m pip install -r requirements.txt
python -m scripts.fetch_models --skip-optional   # then add yolov8n-face.pt by hand (step 3d)
python -m app.manage create-admin --email you@example.com
python -m app.serve                           # http://127.0.0.1:8822
```

Running the tests also needs `pip install -r requirements-dev.txt`, then
`python -m pytest`.

## 8. Day-to-day commands

| Task | Command |
|---|---|
| Start / update the backend after pulling code | `docker compose up -d --build` |
| Backend logs | `docker compose logs -f backend` |
| Stop the backend | `docker compose stop` |
| Start the web app | `cd frontend; npm run dev` |
| Back up the database | `docker run --rm -v smc_data:/data -v "${PWD}\backup:/out" alpine cp -a /data/. /out/` |

## 9. Never commit

`.env` files, `backend/data/`, the Docker volume contents, and the model weights.
They hold secrets, the database, and photos of real people. `.gitignore` already
excludes them.
