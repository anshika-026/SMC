#!/usr/bin/env bash
# Restores an SMC data bundle (scripts/smc_make_bundle.sh) ON the GPU server into
# SMC's own folder (~/smc), and proves it matches the source: checksums, database
# integrity, and the row count of every table.
#
#   bash scripts/smc_restore_data.sh <BUNDLE_DIR>
#
# Never overwrites: it refuses if ~/smc/data already holds a database. To restore
# again, move the old data folder aside yourself first (nothing here deletes data).
# Run it while smc.service is stopped (or before the first deploy).
set -euo pipefail

BUNDLE="${1:?usage: smc_restore_data.sh <BUNDLE_DIR>}"
ROOT="${SMC_ROOT:-$HOME/smc}"
DATA="$ROOT/data"
PY="${PYTHON:-python3}"

log() { printf '[restore %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

[ -f "$BUNDLE/SHA256SUMS" ] || { log "no SHA256SUMS in $BUNDLE"; exit 1; }
if [ -e "$DATA/app.db" ]; then
  log "$DATA/app.db already exists; not overwriting it"; exit 1
fi

log "checksums"
(cd "$BUNDLE" && sha256sum -c --quiet SHA256SUMS) || { log "bundle is damaged or incomplete"; exit 1; }

mkdir -p "$DATA" "$ROOT/models" "$ROOT/insightface" "$ROOT/logs" "$ROOT/hf"
chmod 700 "$ROOT" "$DATA"

log "files"
tar -xzf "$BUNDLE/smc_data.tar.gz" -C "$DATA"
cp "$BUNDLE/app.db" "$DATA/app.db"
tar -xzf "$BUNDLE/models.tar.gz" -C "$ROOT"                       # -> $ROOT/models
tar -xzf "$BUNDLE/insightface.tar.gz" -C "$ROOT/insightface" --strip-components=1   # .insightface/* -> insightface/

log "database: integrity and row counts against the source"
"$PY" - "$DATA/app.db" "$BUNDLE/counts.json" <<'PY'
import json, sqlite3, sys
db, counts_path = sys.argv[1], sys.argv[2]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
assert c.execute("pragma integrity_check").fetchone()[0] == "ok", "integrity check FAILED"
want = json.load(open(counts_path))
got = {t: c.execute(f'select count(*) from "{t}"').fetchone()[0]
       for (t,) in c.execute("select name from sqlite_master where type='table'")}
bad = {t: (want.get(t), got.get(t)) for t in set(want) | set(got) if want.get(t) != got.get(t)}
if bad:
    sys.exit(f"row counts differ (source, restored): {bad}")
key = ["employees", "face_embeddings", "face_training_captures", "attendance_days",
       "visitor_report_log", "alerts", "intrusion_events", "cameras", "admin_users"]
print("integrity ok; all", len(got), "tables match:", ", ".join(f"{k} {got.get(k)}" for k in key if k in got))
PY
log "restored into $ROOT (data, models, insightface). Next: backend.env, then the first deploy."
