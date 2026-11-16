#!/usr/bin/env bash
# Deploys one SMC commit ON the shared DeWIN GPU server (user kanishka), the same
# way Deco Vision deploys there (its scripts/deploy_gpu.sh): build beside the
# running release, check models and production config, switch `current`,
# restart, wait for /health and /ready, roll back automatically on failure.
#
#   SHA=<commit> RELEASE_DIR=<unpacked commit> bash scripts/deploy_smc_gpu.sh
#
# Getting a commit onto the server (from a machine with the SMC repo):
#   git archive --format=tar <commit> | ssh kanishka@<server> \
#     'd=~/smc/releases/$(date +%Y%m%d-%H%M%S)-<sha12>; mkdir -p "$d" && tar -x -C "$d"'
#
# Confined to ~/smc: no sudo, no apt, no system services, no nginx reloads; it
# never touches Deco Vision's ~/tatv. The backend is the user unit smc.service on
# 127.0.0.1; nginx serves ~/smc/current/frontend/dist, so switching `current`
# publishes a new frontend atomically.
#
# Layout (~/smc/):
#   releases/<stamp>-<sha12>/  one unpacked commit each (newest KEEP_RELEASES kept)
#   current -> releases/...    what is live
#   venv/                      CUDA torch + onnxruntime-gpu, updated each deploy
#   backend.env                config + secrets (chmod 600, never in git)
#   data/ logs/ models/ insightface/ hf/   persistent state (never inside a release)
set -euo pipefail

: "${SHA:?}" "${RELEASE_DIR:?}"

ROOT="${SMC_ROOT:-$HOME/smc}"
SERVICE="smc.service"
ENV_FILE="$ROOT/backend.env"
VENV="$ROOT/venv"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"   # GPU model warm-up can take a while
KEEP_RELEASES="${KEEP_RELEASES:-5}"

log() { printf '[smc-deploy %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
# shellcheck source=/dev/null  # backend.env exists only on the server
with_env() { (set -a; . "$ENV_FILE"; set +a; "$@"); }

test -f "$ENV_FILE" || { log "missing $ENV_FILE"; exit 1; }
PORT="$(sed -n 's/^PORT=//p' "$ENV_FILE" | tail -1)"
test -n "$PORT" || { log "PORT not set in $ENV_FILE"; exit 1; }
MODELS="$(sed -n 's/^MODEL_DIR=//p' "$ENV_FILE" | tail -1)"
MODELS="${MODELS:-$ROOT/models}"
DATA="$(sed -n 's/^DATA_DIR=//p' "$ENV_FILE" | tail -1)"
test -n "$DATA" && test -f "$DATA/app.db" || {
  log "no SMC database at DATA_DIR ($DATA): restore the data first (scripts/smc_restore_data.sh)"; exit 1; }

PREV_RELEASE="$(readlink -f "$ROOT/current" 2>/dev/null || true)"
PREV_SHA="$(cat "$PREV_RELEASE/DEPLOYED_SHA" 2>/dev/null || echo none)"
log "current ${PREV_SHA:0:12} -> target ${SHA:0:12} ($(basename "$RELEASE_DIR"))"

# NOTE: `set -e` does not apply inside a function used as an `if` condition,
# so every step below fails the function explicitly with `|| return 1`.

install_python() {   # $1 = release dir
  local req="$1/backend/requirements.txt" filtered torch_v tv_v ort_v
  [ -x "$VENV/bin/python" ] || python3.12 -m venv "$VENV" || return 1
  torch_v="$(sed -n 's/^torch==//p' "$req")"
  tv_v="$(sed -n 's/^torchvision==//p' "$req")"
  ort_v="$(sed -n 's/^onnxruntime==//p' "$req")"
  # CUDA builds of torch (Blackwell GPUs, CUDA 13 driver), installed first so
  # requirements.txt's torch==X pin is already satisfied by X+cu130.
  "$VENV/bin/pip" install -q --index-url "$TORCH_INDEX_URL" "torch==$torch_v" "torchvision==$tv_v" || return 1
  # onnxruntime (CPU) and onnxruntime-gpu provide the same module: only the GPU one.
  filtered="$(mktemp)"
  grep -viE '^onnxruntime==' "$req" > "$filtered"
  "$VENV/bin/pip" install -q -r "$filtered" || { rm -f "$filtered"; return 1; }
  rm -f "$filtered"
  if "$VENV/bin/pip" show -q onnxruntime >/dev/null 2>&1; then
    "$VENV/bin/pip" uninstall -y -q onnxruntime || return 1
    "$VENV/bin/pip" install -q --force-reinstall --no-deps "onnxruntime-gpu==$ort_v" || return 1
  fi
  "$VENV/bin/pip" install -q "onnxruntime-gpu==$ort_v" || return 1
  # insightface declares the CPU package name `onnxruntime`; that one report is expected.
  local out
  if ! out="$("$VENV/bin/pip" check 2>&1)"; then
    if grep -vE 'requires onnxruntime, which is not installed' <<<"$out" | grep -q .; then
      printf '%s\n' "$out"; return 1
    fi
  fi
  # The GPU provider must really be there, or face recognition silently runs on CPU.
  "$VENV/bin/python" -c "import onnxruntime as o, sys; sys.exit('CUDAExecutionProvider' not in o.get_available_providers())" || {
    log "onnxruntime has no CUDAExecutionProvider"; return 1; }
}

link_models() {   # $1 = release dir
  # Weights tracked in git join the persistent model dir; the release's
  # backend/models then points at it (some code reads backend/models directly).
  mkdir -p "$MODELS" || return 1
  if [ -d "$1/backend/models" ] && [ ! -L "$1/backend/models" ]; then
    cp -an "$1/backend/models/." "$MODELS/" || return 1
    rm -rf "$1/backend/models" || return 1
  fi
  ln -sfn "$MODELS" "$1/backend/models" || return 1
}

build() {
  log "python dependencies (CUDA torch, onnxruntime-gpu)"
  install_python "$RELEASE_DIR" || return 1

  log "models"
  link_models "$RELEASE_DIR" || return 1
  (cd "$RELEASE_DIR/backend" && with_env "$VENV/bin/python" -m scripts.fetch_models --check >/dev/null) || {
    (cd "$RELEASE_DIR/backend" && with_env "$VENV/bin/python" -m scripts.fetch_models --check) || true
    log "a required model is missing from $MODELS"; return 1; }

  log "production config"
  (cd "$RELEASE_DIR/backend" && with_env "$VENV/bin/python" -m app.manage check-config) || return 1

  log "frontend build (frontend/.env.production: relative /api, the full module list)"
  (cd "$RELEASE_DIR/frontend" && npm ci --silent --no-audit --no-fund && npx vite build --logLevel warn) || return 1
  test -f "$RELEASE_DIR/frontend/dist/index.html" || { log "frontend build produced no index.html"; return 1; }
  printf '%s\n' "$SHA" > "$RELEASE_DIR/DEPLOYED_SHA"
}

switch_to() {   # $1 = release dir
  ln -sfn "$1" "$ROOT/current.tmp" && mv -Tf "$ROOT/current.tmp" "$ROOT/current"
}

restart_and_verify() {
  log "restart $SERVICE"
  systemctl --user restart "$SERVICE" || return 1
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) body
  until curl -fsS -m 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; do
    if ! systemctl --user is-active -q "$SERVICE"; then
      log "service is not running:"; journalctl --user -u "$SERVICE" -n 30 --no-pager || true; return 1
    fi
    [ $SECONDS -lt $deadline ] || { log "/health did not come up in ${HEALTH_TIMEOUT}s"; return 1; }
    sleep 3
  done
  until body="$(curl -fsS -m 10 "http://127.0.0.1:${PORT}/ready" 2>/dev/null)"; do
    [ $SECONDS -lt $deadline ] || {
      log "not ready: $(curl -sS -m 10 "http://127.0.0.1:${PORT}/ready" || true)"; return 1; }
    sleep 3
  done
  log "ready: ${body:0:300}"
  systemctl --user enable -q "$SERVICE" || true
}

rollback() {
  if [ -z "$PREV_RELEASE" ] || [ ! -d "$PREV_RELEASE" ]; then
    log "no previous release to roll back to"; return
  fi
  log "ROLLING BACK to ${PREV_SHA:0:12}"
  switch_to "$PREV_RELEASE" || true
  install_python "$PREV_RELEASE" || true
  systemctl --user restart "$SERVICE" || true
}

prune() {
  local keep cur
  cur="$(readlink -f "$ROOT/current")"
  keep="$(ls -1dt "$ROOT"/releases/*/ 2>/dev/null | head -n "$KEEP_RELEASES" | xargs -r -n1 readlink -f)"
  for d in "$ROOT"/releases/*/; do
    d="$(readlink -f "$d")"
    [ "$d" = "$cur" ] || [ "$d" = "$PREV_RELEASE" ] || grep -qxF "$d" <<<"$keep" || rm -rf "$d"
  done
}

if build; then
  switch_to "$RELEASE_DIR"
  if restart_and_verify; then
    prune
    log "deployed ${SHA:0:12} (previous ${PREV_SHA:0:12})"
    exit 0
  fi
  rollback
else
  log "build failed; the running release was not touched"
fi
[ "$(readlink -f "$ROOT/current" 2>/dev/null)" = "$(readlink -f "$RELEASE_DIR")" ] || rm -rf "$RELEASE_DIR"
log "deploy of ${SHA:0:12} FAILED"
exit 1
