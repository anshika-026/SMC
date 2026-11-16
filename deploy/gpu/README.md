# SMC on the shared DeWIN GPU server

SMC runs on the same GPU server as Deco Vision (`tatv.dewin.in`), as its own
application, set up the way Deco Vision is (`deploy/gpu` in the deco-vision repo):
a systemd **user** service bound to 127.0.0.1, nginx in front, everything under
`/home/kanishka/smc`, nothing under Deco Vision's `~/tatv`.

| | Deco Vision (exists) | SMC (this folder) |
|---|---|---|
| Address | https://tatv.dewin.in | https://smc.dewin.in |
| Backend | `tatv@prod`, 127.0.0.1:18811 | `smc.service`, 127.0.0.1:18821 |
| Files | `~/tatv/prod` | `~/smc` |
| RTMP ingest | — | `smc-mediamtx.service`, :1935 (office only) |

The client URL the Master Portal generated, `https://tatv.dewin.in/client/smc/login`,
checks the SMC licence and forwards to `https://smc.dewin.in/sso`; SMC then shows
its own login (its email and password are the only login).

SMC needs its own hostname: SMC and Deco Vision keep their browser sessions under
the same localStorage keys, so on one origin they would overwrite each other's logins.

## Files

| File | What |
|---|---|
| `smc.service` | backend user unit (MemoryMax 12G, CPUQuota 600%) |
| `smc-mediamtx.service`, `mediamtx-smc.yml` | RTMP ingest; RTSP and API on 127.0.0.1 only |
| `nginx-smc.conf` | the `smc.dewin.in` site (adds a site; touches no other) |
| `backend.env.template` | settings, placeholders only |
| `../../scripts/deploy_smc_gpu.sh` | build, switch, restart, health check, automatic rollback |
| `../../scripts/smc_make_bundle.sh` | data bundle from the running laptop SMC (read-only) |
| `../../scripts/smc_restore_data.sh` | restores a bundle on the server and proves it matches |

## Procedure

Every step marked **[approval]** changes something outside this repo and needs the
owner's explicit go-ahead first.

1. **Capacity check** (read-only):
   `nvidia-smi`; `nvidia-smi --query-compute-apps=gpu_uuid,process_name,used_memory --format=csv`;
   `nproc; free -h; df -h /home`; `sudo ss -ltn | grep -E ':(1935|18554|18821|19997)\b'` (must be free).
   SMC needs about 3–5 GB of GPU memory, 4–6 cores, 4–6 GB RAM and 50 GB of disk.
   Pick the GPU with the most free memory for `CUDA_VISIBLE_DEVICES`.
2. **[approval] DNS**: A record `smc.dewin.in` → the server's IP (same IP as tatv.dewin.in).
3. **[approval] nginx + certificate** (sudo, once): see the header of `nginx-smc.conf`.
4. **[approval] Firewall**: allow TCP 1935 from the office's public IP only (for the
   relayed cameras). CAM1–CAM4 are pulled by SMC and need no inbound port.
5. **Folders and services** (as kanishka, no sudo):
   ```bash
   mkdir -p ~/smc/{releases,mediamtx} ~/.config/systemd/user
   cp deploy/gpu/smc.service deploy/gpu/smc-mediamtx.service ~/.config/systemd/user/
   cp deploy/gpu/mediamtx-smc.yml ~/smc/mediamtx.yml
   # MediaMTX v1.9.3 (linux amd64) from github.com/bluenviron/mediamtx/releases -> ~/smc/mediamtx/mediamtx
   systemctl --user daemon-reload
   ```
6. **[approval] Data**: on the laptop, `bash scripts/smc_make_bundle.sh` (reads the
   running SMC only), copy the bundle over SSH (`scp -r`), then on the server
   `bash scripts/smc_restore_data.sh <bundle>`. It refuses to overwrite and fails
   unless checksums, integrity and every table's row count match.
7. **Settings**: `~/smc/backend.env` from `backend.env.template` (chmod 600) and
   `~/smc/mediamtx.env` (chmod 600). `SMC_CREDENTIALS_KEY`, `INGEST_HOOK_SECRET` and
   `INGEST_READ_PASSWORD` come from the laptop's `.env` files, typed in, never copied
   through git or chat.
8. **[approval] Licence link** (production Master Portal, after Deco Vision's
   develop → main release):
   1. On the portal server: `python -m app.manage generate-sso-key` (prints the public key).
   2. Client License Management → the SMC licence → Application: **SMC**,
      address **https://smc.dewin.in**, the 7 SMC features; generate the app key (shown once).
   3. Put the licence id, app key and public key into `~/smc/backend.env`.
9. **[approval] First deploy**: unpack the commit (see the header of
   `scripts/deploy_smc_gpu.sh`), then `SHA=... RELEASE_DIR=... bash scripts/deploy_smc_gpu.sh`.
   `systemctl --user enable --now smc-mediamtx` once.
10. **Cutover**: stop the laptop SMC (`docker stop smc-backend smc-mediamtx`;
    nothing is deleted), make a final bundle, restore it (move the trial data aside
    first), restart `smc.service`. Never run both SMCs at once: alerts and
    WhatsApp messages would go out twice.

## Acceptance test (on the real URL)

1. `https://tatv.dewin.in/client/smc/login` forwards to `https://smc.dewin.in/sso`.
2. SMC's own login appears; a wrong password is refused.
3. The SMC email and password open SMC's dashboard.
4. The licensed modules appear: Face Recognition, Face Training, Attendance,
   Visitor Management, Footfall, Workforce, Intrusion.
5. Existing data is there: compare counts with the bundle's `counts.json`.
6. Live video and staff counts update (WebSockets over wss://).
7. Suspend the licence: the client URL and open sessions are refused within a
   minute; reactivate: access returns.
8. WELMONT's client login page still shows its own form.

## Operations

```bash
systemctl --user status smc smc-mediamtx
journalctl --user -u smc -f
curl -s 127.0.0.1:18821/ready        # includes the licence state: ok / grace / locked
```

Rollback: point `~/smc/current` at an older release and `systemctl --user restart smc`.
Back to the laptop: `systemctl --user stop smc smc-mediamtx`, then `docker start
smc-backend smc-mediamtx` on the laptop. If the server SMC ran for a while, first
copy its database back (SQLite online backup of `~/smc/data/app.db`, as
`smc_make_bundle.sh` does) and its new files, so no records are lost; that copy-back
is a manual, separately approved step.
