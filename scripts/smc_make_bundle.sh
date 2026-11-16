#!/usr/bin/env bash
# Makes a data bundle of the RUNNING SMC (Docker, volume smc_data) for moving it to
# the GPU server (scripts/smc_restore_data.sh). Run in Git Bash on the machine that
# runs SMC today. It only READS: the live volume, models and code are not changed.
#
#   bash scripts/smc_make_bundle.sh [OUT_DIR]      (default C:/SMC-backups/<date>-bundle)
#
# The bundle holds face images, embeddings and other personal data: keep it on
# disks only you can read, never in git, and move it only over SSH.
#
# Contents (all checked by SHA256SUMS):
#   app.db               consistent copy made with SQLite's online backup API
#   counts.json          row count of every table at bundle time (restore compares)
#   smc_data.tar.gz      every other file in the volume (captures, training, clips...)
#   models.tar.gz        the container's /app/models (YOLO, face detector, Re-ID, emotion)
#   insightface.tar.gz   the container's /insightface (buffalo_l face recognition)
# Models come from the running container, not from this checkout: a git clone
# has no model weights (they are not in git).
# Secrets (.env files) are NOT included: they are typed into ~/smc/backend.env.
set -euo pipefail

CONTAINER="${SMC_CONTAINER:-smc-backend}"
OUT="${1:-/c/SMC-backups/$(date +%Y-%m-%d-%H%M)-bundle}"

log() { printf '[bundle %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
# docker cp on Windows needs a Windows path (C:\...), not Git Bash's /c/...
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

[ -e "$OUT" ] && { log "$OUT already exists; not overwriting it"; exit 1; }
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true || {
  log "container $CONTAINER is not running"; exit 1; }
mkdir -p "$OUT"

log "database (online backup, consistent while SMC runs)"
MSYS_NO_PATHCONV=1 docker exec -i "$CONTAINER" python - <<'PY'
import json, sqlite3
src = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
dst = sqlite3.connect("/tmp/smc-bundle.db")
src.backup(dst)
dst.close()
c = sqlite3.connect("/tmp/smc-bundle.db")
assert c.execute("pragma integrity_check").fetchone()[0] == "ok", "integrity check failed"
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' order by name")]
counts = {t: c.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
json.dump(counts, open("/tmp/smc-bundle-counts.json", "w"), indent=1, sort_keys=True)
print(f"{len(tables)} tables, integrity ok")
PY
MSYS_NO_PATHCONV=1 docker cp "$CONTAINER:/tmp/smc-bundle.db" "$(hostpath "$OUT/app.db")"
MSYS_NO_PATHCONV=1 docker cp "$CONTAINER:/tmp/smc-bundle-counts.json" "$(hostpath "$OUT/counts.json")"
MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" rm -f /tmp/smc-bundle.db /tmp/smc-bundle-counts.json

log "volume files (database excluded: app.db above is the consistent copy)"
MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" tar -C /data -czf - \
  --exclude=./app.db --exclude='./app.db-*' --exclude='./app.db.bak*' . > "$OUT/smc_data.tar.gz"

log "models and InsightFace (exactly what the running container uses)"
MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" tar -C /app -czf - models > "$OUT/models.tar.gz"
MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" sh -c 'cd / && tar -czf - --transform "s,^insightface,.insightface," insightface' > "$OUT/insightface.tar.gz"
for f in models insightface; do
  [ "$(tar -tzf "$OUT/$f.tar.gz" | grep -c -v '/$')" -gt 0 ] || { log "$f.tar.gz is empty"; exit 1; }
done

(cd "$OUT" && sha256sum app.db counts.json smc_data.tar.gz models.tar.gz insightface.tar.gz > SHA256SUMS)
log "done: $OUT ($(du -sh "$OUT" | cut -f1)); verify with: (cd \"$OUT\" && sha256sum -c SHA256SUMS)"
