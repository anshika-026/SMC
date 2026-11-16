# SMC on the shared DeWIN GPU server

SMC runs on the same GPU server as Deco Vision, as its own application, set up the
way Deco Vision is (`deploy/gpu` in the deco-vision repo): a systemd **user** service
bound to 127.0.0.1 behind nginx, everything under `/home/kanishka/smc`, nothing under
Deco Vision's `~/tatv`.

It is served at **`https://tatv.dewin.in/smc/`**, inside the existing tatv.dewin.in
site: no new DNS record, certificate or site. The Master Portal's client URL
`https://tatv.dewin.in/client/smc/login` checks the SMC licence and forwards to
`https://tatv.dewin.in/smc/sso`; SMC then shows its own login (its email and password
are the only login).

SMC keeps its browser session under its own keys (`smc_token`, `smc_user`), so it and
the portal can share the origin without overwriting each other's logins
(`frontend/src/lib/appBase.js`).

| | Deco Vision (exists) | SMC |
|---|---|---|
| Address | https://tatv.dewin.in/ | https://tatv.dewin.in/smc/ |
| Backend | `tatv@prod`, 127.0.0.1:18811 | `smc.service`, 127.0.0.1:18821 |
| Files | `~/tatv/prod` | `~/smc` |
| RTMP ingest | — | `smc-mediamtx.service`, :1935 (office cameras, later) |

**To deploy, follow [`ADMIN_CHECKLIST.md`](ADMIN_CHECKLIST.md)** (one document, in order).

## Files

| File | What |
|---|---|
| `ADMIN_CHECKLIST.md` | the complete one-time setup, step by step, with who does what |
| `smc.service` | backend user unit (MemoryMax 12G, CPUQuota 600%) |
| `smc-mediamtx.service`, `mediamtx-smc.yml` | RTMP ingest; RTSP and API on 127.0.0.1 only |
| `nginx-smc-location.conf` | the `/smc/` locations, included in the existing tatv.dewin.in site |
| `backend.env.template` | settings, placeholders only |
| `../../scripts/deploy_smc_gpu.sh` | build, switch, restart, health check, automatic rollback |
| `../../scripts/smc_make_bundle.sh` | data bundle from the running laptop SMC (read-only) |
| `../../scripts/smc_restore_data.sh` | restores a bundle on the server and proves it matches |

## Operations

```bash
systemctl --user status smc smc-mediamtx
journalctl --user -u smc -f
curl -s 127.0.0.1:18821/ready        # includes the licence state: ok / grace / locked
```

Later deploys: unpack the new commit into `~/smc/releases/<stamp>-<sha12>/` and run
`SHA=<commit> RELEASE_DIR=<that dir> bash <that dir>/scripts/deploy_smc_gpu.sh`.
Rollback: point `~/smc/current` at an older release and `systemctl --user restart smc`.
