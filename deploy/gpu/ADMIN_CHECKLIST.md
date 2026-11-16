# SMC on the DeWIN GPU server: administrator checklist

**Goal:** `https://tatv.dewin.in/client/smc/login` opens the original SMC (its own
login, dashboard, data and modules), served at `https://tatv.dewin.in/smc/`.

**What this does NOT need:** a new server, a new DNS record, a new certificate or a
new nginx site. SMC runs beside Deco Vision exactly as Deco Vision runs: a systemd
**user** service as `kanishka` on 127.0.0.1, behind the existing `tatv.dewin.in` site.

**Who does what**

| Step | Who | Server access |
|---|---|---|
| 1–2, 4–6, 8 | Server administrator | ssh as `kanishka` (no sudo) |
| 7 | Server administrator | **sudo, once** (one nginx include line + reload) |
| 3, 9, 10 | SMC owner (Anshika) | none (laptop and portal admin) |

**Before you start**
- Deco Vision PR #8 (develop → main) is merged and its production deploy is green.
- SMC PRs are merged into `feature/visitor-history-dates` (the code that is deployed).
- About 60–90 minutes. Nothing here touches Deco Vision's `~/tatv` or its database.
  The steps below that do touch shared things are marked **shared**.

---

## 1. Capacity check (read-only, 2 min)

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv
nvidia-smi --query-compute-apps=gpu_uuid,process_name,used_memory --format=csv
nproc; free -h; df -h /home; uptime
ss -ltn | grep -E ':(1935|18554|18821|19997)\b'      # must print NOTHING (ports free)
python3.12 --version; node --version
```

Go ahead only if: one GPU has **≥ 5 GB free**, there are **≥ 6 cores** and **≥ 6 GB RAM**
free, and `/home` has **≥ 50 GB** free. Note the GPU index with the most free memory
(for `CUDA_VISIBLE_DEVICES`). If a port is taken, stop and report it (the SMC port can move).

## 2. Folders, services, MediaMTX (as kanishka, 5 min)

```bash
mkdir -p ~/smc/{releases,mediamtx,logs,hf} ~/.config/systemd/user
chmod 700 ~/smc
cd ~/smc/mediamtx
curl -fsSLO https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_amd64.tar.gz
# v1.9.3 publishes no checksum file; this is the SHA-256 of the official download:
echo "0b885dbfa4ef9c14cd00191c57d90d804255ff50403a28b85ceee7988c535b60  mediamtx_v1.9.3_linux_amd64.tar.gz" | sha256sum -c -
tar -xzf mediamtx_v1.9.3_linux_amd64.tar.gz mediamtx && rm mediamtx_v1.9.3_linux_amd64.tar.gz
```

## 3. The release and the data bundle (SMC owner, on the laptop)

```bash
# in the SMC repo, at the commit to deploy (feature/visitor-history-dates):
git archive --format=tar.gz -o smc-release.tar.gz HEAD
git rev-parse HEAD                                  # give this commit id to the administrator (step 6)
# read-only copy of the running SMC's data (database, files, models, InsightFace):
bash scripts/smc_make_bundle.sh                    # -> C:/SMC-backups/<date>-bundle
scp smc-release.tar.gz kanishka@<server>:~/smc/
scp -r C:/SMC-backups/<date>-bundle kanishka@<server>:~/smc-bundle
```

The bundle holds face images and embeddings: SSH only, never e-mail, chat or GitHub.
Secrets (`SMC_CREDENTIALS_KEY`, `INGEST_HOOK_SECRET`, `INGEST_READ_PASSWORD`) are handed
over separately and only typed into the files in step 5.

## 4. Unpack and restore the data (as kanishka, 10 min)

```bash
REL=~/smc/releases/$(date +%Y%m%d-%H%M%S)-first && mkdir -p "$REL"
tar -xzf ~/smc/smc-release.tar.gz -C "$REL"
bash "$REL/scripts/smc_restore_data.sh" ~/smc-bundle
```

It must end with `integrity ok; all N tables match`. It refuses to overwrite an existing
`~/smc/data` and refuses a damaged bundle; nothing is deleted.

## 5. Settings (as kanishka, 5 min)

```bash
cp "$REL/deploy/gpu/backend.env.template" ~/smc/backend.env && chmod 600 ~/smc/backend.env
nano ~/smc/backend.env          # fill every <...>; MASTER_* values come in step 9
printf 'MTX_AUTHHTTPADDRESS=http://127.0.0.1:18821/api/ingest/auth?secret=%s\n' '<INGEST_HOOK_SECRET>' > ~/smc/mediamtx.env
chmod 600 ~/smc/mediamtx.env
cp "$REL/deploy/gpu/mediamtx-smc.yml" ~/smc/mediamtx.yml
cp "$REL/deploy/gpu/smc.service" "$REL/deploy/gpu/smc-mediamtx.service" ~/.config/systemd/user/
systemctl --user daemon-reload
```

Until step 9, set `INTEGRATION_MODE=standalone` in `backend.env` so SMC can start
before the licence is linked (nobody can reach it yet: nginx is not set up).

## 6. Deploy SMC (as kanishka, 15–25 min: CUDA libraries download the first time)

```bash
systemctl --user enable --now smc-mediamtx
SHA=<commit id from step 3> RELEASE_DIR="$REL" bash "$REL/scripts/deploy_smc_gpu.sh"
```

It installs the Python and frontend dependencies, checks models and config, starts
`smc.service` and waits for `/health` and `/ready`; if anything fails it says why and,
on a first deploy, leaves nothing live.
Then check the cameras and the GPU:

```bash
curl -s 127.0.0.1:18821/ready | python3 -m json.tool | grep -E '"camera_id"|"state"'
journalctl --user -u smc | grep "face recognition runs on"    # expect: the GPU
nvidia-smi --query-compute-apps=process_name,used_memory --format=csv
```

CAM1–CAM4 (public RTSP) should reach `RUNNING`. The three office-LAN cameras stay
`RECONNECTING` until the office relay exists (later; not needed for go-live).

## 7. nginx: route /smc/ to SMC (**sudo, once, shared**)

```bash
sudo cp "$REL/deploy/gpu/nginx-smc-location.conf" /etc/nginx/snippets/smc-locations.conf
sudoedit /etc/nginx/sites-available/tatv
#   inside the tatv.dewin.in server block that listens on 443, add one line:
#       include snippets/smc-locations.conf;
sudo nginx -t && sudo systemctl reload nginx
chmod o+x /home/kanishka /home/kanishka/smc          # nginx reads the frontend
curl -sI https://tatv.dewin.in/smc/ | head -1        # 200
curl -s  https://tatv.dewin.in/ | grep -c assets     # Deco Vision unchanged (>0)
```

Only `/smc/...` is added; every existing Deco Vision path stays as it was. To undo:
remove the include line, `sudo nginx -t && sudo systemctl reload nginx`.

## 8. Signing key on the production portal (as kanishka, 1 min, **shared**)

```bash
cd ~/tatv/prod/current/backend
(set -a; . ~/tatv/prod/backend.env; set +a; ~/tatv/prod/venv/bin/python -m app.manage generate-sso-key)
```

Prints the PUBLIC key (give it to the SMC owner; the private key stays in
`~/tatv/prod/data/sso_signing_key.pem`). It refuses to overwrite an existing key.
No Deco Vision restart is needed.

## 9. Link the licence (SMC owner, in the portal, 5 min)

Client License Management → the SMC licence → Application: **SMC**, address
**`https://tatv.dewin.in/smc`**, tick the 7 SMC features, camera limit ≥ 7,
**Generate application key** (shown once). Hand the licence id, app key and the public
key from step 8 to the administrator, who sets in `~/smc/backend.env`:

```
INTEGRATION_MODE=master
MASTER_LICENSE_ID=<licence id>
MASTER_APP_API_KEY=<dvk_...>
MASTER_PORTAL_PUBLIC_KEY=<public key from step 8>
```

then `systemctl --user restart smc` and checks `curl -s 127.0.0.1:18821/ready` shows
`"license": {"state": "ok", ...}`.

## 10. Cutover and acceptance (SMC owner)

1. Stop the laptop SMC so only one SMC runs: `docker stop smc-backend smc-mediamtx`
   (nothing is deleted; `docker start` brings it back).
   If the laptop collected new records since step 3, repeat steps 3–4 with a new
   bundle into a fresh `~/smc/data` (move the trial one aside) before going live.
2. Open **https://tatv.dewin.in/client/smc/login** → SMC's own login appears.
3. Wrong password refused; the SMC email and password open SMC's dashboard.
4. Face Recognition, Face Training, Attendance, Visitors, Footfall, Workforce and
   Intrusion are there, with the existing data.
5. Live video and staff counts update.
6. Suspend the licence → the URL and open sessions are refused within a minute;
   reactivate → access returns.
7. A WELMONT client login still shows its own form; Deco Vision admin works.

## Rollback

| Undo | How |
|---|---|
| nginx | remove the include line, `sudo nginx -t && sudo systemctl reload nginx` |
| SMC service | `systemctl --user disable --now smc smc-mediamtx` |
| Back to the laptop | `docker start smc-backend smc-mediamtx` on the laptop |
| Licence | set the licence's Application back to Deco Vision (clears its key and address) |

Nothing in this checklist deletes data; the laptop SMC and its `smc_data` volume stay
as they are throughout.
