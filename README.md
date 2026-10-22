# SMC Client

The MAINDV (Deco Vision) application, deployed for SMC with **Unique Footfall
as the only AI feature**. Dashboard, Live Feed, Alerts, Camera Management,
Site Management, Client Licenses and Settings work as in MAINDV; every other
AI feature (face recognition, attendance, staff count, workforce, intrusion,
object detection, AI live overlay) is switched off in the backend and hidden
in the frontend.

## Feature switch

| Where | Setting | SMC value |
|---|---|---|
| `backend/.env` | `ENABLED_FEATURES` | `footfall` |
| `frontend/.env.production` (and `frontend/.env` for dev) | `VITE_ENABLED_FEATURES` | `footfall` |

Both lists must match. Empty = the full MAINDV product.

## Setup

Backend (Python 3.12, see `DEPLOYMENT` notes in MAINDV):

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate       # Linux: . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0
pip install -r requirements.txt
cp .env.example .env            # set JWT_SECRET, ENABLED_FEATURES=footfall
python -m app.manage create-admin --email you@example.com
python -m scripts.fetch_reid_model
python -m app.serve             # http://127.0.0.1:8821
```

Frontend:

```bash
cd frontend
cp .env.example .env
npm ci
npm run dev                     # http://localhost:5180
```

## SMC cameras

Add both in **Camera Management** with purpose **Entry/Exit** (Entry/Exit
cameras are the ones unique footfall counts), site **SMC**:

| Name | Host | Port | Stream path |
|---|---|---|---|
| SMC Camera 1 | 144.79.198.149 | 8554 | `/video/live?channel=1&subtype=0` |
| SMC Camera 2 | 144.79.198.149 | 8555 | `/video/live?channel=1&subtype=0` |

User `admin`; the password is shared separately (never commit it). Then draw
each camera's doorway zone on **Footfall UAT**.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

`docs/UNIQUE_FOOTFALL.md` explains the Re-ID engine behind unique footfall.
