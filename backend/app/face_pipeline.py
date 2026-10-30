"""
face_pipeline.py

Per-camera worker: detect -> track -> pick best frame per track -> align ->
embed (ArcFace) -> match against gallery -> auto-tag or push to review queue.

Design choices, and why:
  - YOLOv8-face for detection: fast, GPU-friendly, good on RTSP streams.
  - ByteTrack for tracking: gives each face a stable track_id across frames
    so we only run recognition ONCE per track (on the best frame) instead of
    on every frame. This is the main latency fix vs. a naive per-frame loop.
    Uses the `supervision` package's ByteTrack implementation rather than
    the original `yolox` package — `yolox` ships a native extension that
    requires cmake/a C++ toolchain to build and failed to install on this
    box; `supervision` is a pure-Python/numpy port of the same algorithm
    with no native build step, so it's the safer dependency here.
  - InsightFace (buffalo_l, ArcFace) for embeddings: we do NOT train/retrain
    this model. It's a fixed embedding extractor. "Training" = appending
    vectors to a person's bucket in the gallery (see face_db.py). This also
    means alignment matters a lot — buffalo_l's detector gives 5-point
    landmarks used for alignment before embedding, which was almost
    certainly missing from the old broken pipeline (raw bbox crops fed
    straight into ArcFace produce noisy embeddings -> everything reads as
    "unknown").

Install:
    pip install ultralytics insightface onnxruntime supervision opencv-python
    # swap onnxruntime for onnxruntime-gpu if this machine actually has CUDA

Model files:
    - YOLO face weights: any yolov8n-face / yolov11n-face .pt (fine-tuned for
      faces, not the generic COCO yolov8n.pt — that only detects "person").
      NOT bundled here and not auto-downloaded — see FACE_RECOGNITION_WIRING.md
      for why this is a manual step.
    - InsightFace buffalo_l downloads automatically on first run to
      ~/.insightface/models/buffalo_l.
"""

import logging
import os
import queue
import shutil
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import supervision as sv

from ultralytics import YOLO
import insightface

from app import config as _app_config, storage
from app import alerts
from app import analytics_settings
from app import appearance
from app import attendance
from app import desks
from app import face_db
from app import face_matching
from app import employee_directory, face_dataset
from app import expression

log = logging.getLogger("face_pipeline")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Resolved relative to this file (matches camera_db.py / face_db.py's
# DB_PATH convention), NOT the process's cwd — uvicorn for this project is
# always launched from inside backend/ (see BACKEND_HANDOFF.md), so a plain
# "backend/data/..." relative default silently doubled up into a
# backend/backend/data/... tree. Env var overrides still work as absolute
# paths if you need to point somewhere else entirely.
_DATA_DIR = Path(_app_config.DATA_DIR)

# Stable, version-controllable location for model checkpoints — separate
# from data/ (runtime state: captures, the DB, the trained classifier).
# Same cwd-independence reasoning as _DATA_DIR above. The .pt file itself is
# NOT committed to git or fetched automatically (see FACE_RECOGNITION_WIRING.md)
# — it's a deliberate, one-time manual step.
_MODELS_DIR = Path(_app_config.MODEL_DIR)
_MODELS_DIR.mkdir(parents=True, exist_ok=True)

YOLO_FACE_WEIGHTS = os.environ.get("YOLO_FACE_WEIGHTS", str(_MODELS_DIR / "yolov8n-face.pt"))
# Standard Ultralytics COCO-pretrained checkpoint (class 0 = "person") — the
# generic detector the face-tuned one above is explicitly NOT used as (see
# module docstring). Auto-downloaded by ultralytics on first load if not
# already sitting in models/, same as any other stock YOLOv8 weight name.
YOLO_PERSON_WEIGHTS = os.environ.get("YOLO_PERSON_WEIGHTS", str(_MODELS_DIR / "yolov8n.pt"))
SAMPLE_FPS = float(os.environ.get("FACE_PIPELINE_FPS", "3"))  # frames/sec to run detection on
MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.45"))  # cosine sim, tune per data

# Cosine similarity needed to NAME someone on the live overlay from the enrolled
# gallery (the review/auto-assign tiers keep MATCH_THRESHOLD above). 0.45 was
# never measured. scripts/eval_face_matching.py on labelled CCTV captures of
# enrolled people found same-person scores around 0.31-0.46 (median) and scores
# against other people mostly under 0.25-0.29 (p95), so 0.45 rejected most real
# reads. 0.35 keeps a clear gap above other people while accepting far more
# genuine reads; a name still needs RECOGNITION_STABILITY_FRAMES agreeing reads
# and a clear lead over the runner-up (face_matching.MATCH_MARGIN), and attendance
# has its own stricter bar. Re-run the script after enrolling more people.
LIVE_MATCH_THRESHOLD = float(os.environ.get("FACE_LIVE_MATCH_THRESHOLD", "0.35"))

# Opt-in recognition diagnostics (FACE_DIAG=1): one rate-limited log line per
# camera and rejection reason, never an image, a URL or a name. Counters are
# always kept (cheap); see CameraFacePipeline.diag_snapshot().
FACE_DIAG = os.environ.get("FACE_DIAG", "0").lower() in ("1", "true", "yes", "on")
FACE_DIAG_INTERVAL_SECONDS = float(os.environ.get("FACE_DIAG_INTERVAL_SECONDS", "10"))

# When InsightFace finds nothing in a YOLO-detected face crop (30-56% of captures
# on these cameras), try ONCE more with grey background added around the crop.
# Measured on 80 such crops (median short side ~91 px): 5% gave an embedding
# as-is, 51% with the border, at detector confidence 0.57 (the default 0.5 gate
# is kept; lowering it to 0.3 recovered 64% but only adds low-confidence faces
# that match nobody). Runs only after a failure, and at most once per
# FACE_EMBED_RETRY_SECONDS per person track, so frames that already work cost
# nothing and an unrecognisable face is not retried every cycle.
# FACE_EMBED_RETRY=0 turns it off.
FACE_EMBED_RETRY = os.environ.get("FACE_EMBED_RETRY", "1").lower() in ("1", "true", "yes", "on")
FACE_EMBED_RETRY_SECONDS = float(os.environ.get("FACE_EMBED_RETRY_SECONDS", "3"))
FACE_EMBED_RETRY_BORDER = 0.5
TRACK_MAX_AGE = 30  # frames a track can go unseen before it's dropped

# Fraction of the YOLO box's width/height added on each side before cropping
# for InsightFace. Confirmed via scripts/verify_face_detector.py: YOLO found
# a real face at 0.860 confidence, but FaceAnalysis.get() on that exact
# bbox-tight crop found none — InsightFace's own detector (SCRFD, run again
# inside _embed() to get real 5-point landmarks for alignment) needs some
# surrounding context and reliably fails when the face fills the entire
# frame with zero margin. This does not change what YOLO detected or how
# tracking/quality-scoring works — only how much surrounding image ships to
# the alignment step that was already there.
FACE_CROP_MARGIN = float(os.environ.get("FACE_CROP_MARGIN", "0.4"))
CAPTURE_DIR = os.environ.get("FACE_CAPTURE_DIR", str(_DATA_DIR / "face_captures"))
REVIEW_DEDUPE_SECONDS = 60  # don't re-queue the same track more than once per minute

# Bulk dataset collection for manual labeling + classifier training — every
# finished track lands here regardless of match confidence, unlike
# face_pending above (low-confidence only). Labeled captures get moved into
# <employee_id>/ subfolders by face_training_routes.py; this is just the
# unlabeled staging area.
TRAINING_CAPTURE_DIR = os.environ.get("FACE_TRAINING_DIR", str(_DATA_DIR / "face_training"))
TRAINING_UNLABELED_DIR = os.path.join(TRAINING_CAPTURE_DIR, "_unlabeled")

# --- Multi-day collection controls (see FACE_TRAINING.md "7-day collection
# sessions") — unchanged from the shorter-run design except where noted. ---

# Hard ceiling across ALL cameras combined, ALL statuses combined — enforced
# atomically in face_db.py (capture_limit_lock + count_training_captures())
# so concurrent camera threads can never overshoot it. Deliberately left at
# 15000 for a 7-day run too, per explicit instruction not to just raise this
# — a week-long dataset is meant to stay useful/diverse within this budget
# (see the dedup and per-camera controls below), not grow unbounded with run
# length.
MAX_TRAINING_CAPTURES = int(os.environ.get("MAX_TRAINING_CAPTURES", "15000"))

# Per-camera share of that same budget — stops one busy camera (e.g. a main
# entrance) from consuming the entire 15000-capture allowance before quieter
# cameras contribute anything. Default: 1/3 of the global limit, so no
# single camera can dominate even if only 2-3 cameras are actually active,
# while still leaving room for one camera to run alone if that's all that's
# configured. Enforced the same atomic way as MAX_TRAINING_CAPTURES.
MAX_CAPTURES_PER_CAMERA = int(os.environ.get("FACE_MAX_CAPTURES_PER_CAMERA", str(MAX_TRAINING_CAPTURES // 3)))

# Cross-track duplicate protection: if a new capture's embedding is at least
# DEDUP_SIMILARITY_THRESHOLD cosine-similar to one captured on the SAME
# camera within the last DEDUP_COOLDOWN_SECONDS, it's treated as almost
# certainly the same physical appearance re-surfacing (ByteTrack losing and
# re-acquiring the same person, brief occlusion, a quick RTSP reconnect) and
# is dropped before ever touching disk/DB. Deliberately per-camera — this is
# NOT identity recognition (no gallery/employee_id involved) and must never
# suppress the same real person showing up again later or on a different
# camera. See _is_recent_duplicate().
#
# Default raised from 120s (the original single-day design) to 900s (15
# min) for multi-day runs: at 120s, someone with even mildly flickery
# tracking sitting in one spot all day could still generate a new capture
# every ~2 minutes — up to ~240/day, times 7 days, of what's essentially one
# person. 15 minutes caps that to at most ~32/day per person per camera
# while still capturing them several times across a day (different times,
# lighting, pose) rather than just once — the "diverse, not just numerous"
# balance the 7-day plan calls for.
DEDUP_COOLDOWN_SECONDS = float(os.environ.get("FACE_DEDUP_COOLDOWN_SECONDS", "900"))
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("FACE_DEDUP_SIMILARITY_THRESHOLD", "0.7"))

# Default session length for POST /api/faces/training/collection/start when
# no `days` is given. Per explicit instruction, collection must NOT stop on
# its own after a week — the only stop conditions are an explicit
# /collection/stop call, or someone passing an explicit `days` value. This
# stays a finite number (not a null/"forever" sentinel) so the persisted
# session row + expiry watcher in face_collection.py need no special-casing
# for a missing end time; 100 years is indefinite for any practical purpose.
DEFAULT_COLLECTION_DAYS = float(os.environ.get("FACE_COLLECTION_DAYS", "36500"))

# How often the background expiry watcher (face_collection.py) checks
# whether a running session's planned end time has passed.
SESSION_CHECK_INTERVAL_SECONDS = float(os.environ.get("FACE_SESSION_CHECK_INTERVAL", "60"))

# Quality gate applied ONLY to captures that already have an embedding —
# values below are conservative floors/ceiling, deliberately set well
# outside the range actually observed in real test captures (see
# FACE_TRAINING.md for the measured numbers this was based on), so this
# rejects only genuinely degenerate frames, not normal variation.
MIN_BLUR_SCORE = float(os.environ.get("FACE_MIN_BLUR_SCORE", "50"))
MIN_BRIGHTNESS = float(os.environ.get("FACE_MIN_BRIGHTNESS", "20"))
MAX_BRIGHTNESS = float(os.environ.get("FACE_MAX_BRIGHTNESS", "235"))
# Approximate tight-YOLO-bbox area in pixels^2 (not the padded crop area).
MIN_FACE_AREA = float(os.environ.get("FACE_MIN_AREA", "900"))

# Trained classifier (see face_training.py) — frozen ArcFace embeddings stay
# the feature extractor; this is the supervised layer trained on labeled
# camera captures. Absent until you run POST /api/faces/training/train, at
# which point _match() below starts using it automatically.
# Cosine similarity (gallery mode) / probability (classifier mode) needed to
# call a capture's SUGGESTED identity 'reliable' (a suggestion only; a person still approves it). Deliberately above the live
# MATCH_THRESHOLD: naming someone on screen can be a little forgiving, filing
# training data under their ID cannot.
AUTO_ASSIGN_THRESHOLD = float(os.environ.get("FACE_AUTO_ASSIGN_THRESHOLD", "0.55"))
AUTO_ASSIGN_CLASSIFIER_PROBA = float(os.environ.get("FACE_AUTO_ASSIGN_CLASSIFIER_PROBA", "0.9"))
# The best person must beat the runner-up by at least this much, otherwise the
# face is ambiguous between two enrolled people and goes to review.
AUTO_ASSIGN_MARGIN = float(os.environ.get("FACE_AUTO_ASSIGN_MARGIN", "0.08"))
CLASSIFIER_PATH = os.path.join(TRAINING_CAPTURE_DIR, "classifier.joblib")
CLASSIFIER_MIN_PROBA = float(os.environ.get("FACE_CLASSIFIER_MIN_PROBA", "0.6"))

# How often (in *processed*, i.e. already-throttled-to-SAMPLE_FPS frames) a
# still-active person track gets its live-overlay identity re-classified —
# see _update_person_identity(). embedding+predict_proba is the expensive
# step, so this is deliberately not every frame; it only runs at all once a
# classifier has been trained.
#
# Set to match PERSON_DETECT_INTERVAL_FRAMES so a classify attempt happens
# on EVERY overlay pass rather than every other one. At 3 the gap landed
# just above the 2-frame overlay cadence, so each track was only
# classified every ~1.33s, and RECOGNITION_STABILITY_FRAMES=2 agreeing
# reads meant a name took ~2.7s to appear. At 2 that halves to ~0.67s per
# read, so a name shows in ~1.3s — without weakening the stability vote or
# the confidence threshold, which both still have to be satisfied. The
# extra CPU this costs is paid for by the less frequent rescue sweep (see
# PERSON_RESCUE_INTERVAL_FRAMES).
LIVE_CLASSIFY_INTERVAL_FRAMES = int(os.environ.get("FACE_LIVE_CLASSIFY_INTERVAL_FRAMES", "2"))

# --- Person-first live overlay (separate from the face-track pipeline
# above, which keeps building the training/review dataset exactly as
# before — none of this touches that). ---

# Lowered from 0.5 after a real-camera test (2 seated people, backs to the
# camera, upper body partially occluded by monitors/chairs): at the default
# inference resolution, YOLO found the harder of the two at only 0.25-0.35
# confidence — comfortably real, but below the old 0.5 floor, so that
# person got NO box at all despite being clearly, visibly present. Verified
# on an empty-scene frame from the other camera that 0.3 produces zero
# false positives.
PERSON_DETECT_CONF = float(os.environ.get("PERSON_DETECT_CONF", "0.3"))

# YOLO letterboxes/resizes the frame to this size before detection. These
# cameras shoot 1080p; at the library default (640) a seated/occluded
# person's box shrinks to very few pixels and the model's confidence for
# them drops sharply — confirmed directly: the same real person scored 0.25
# at imgsz=640 vs 0.39 at imgsz=960 in a same-frame comparison. 960 was
# chosen over testing an even higher 1280 (which scored slightly better
# still) because it costs ~1.7x a 640 call instead of ~2.8x, for
# comparable real-world recall on the actual failing case — see
# PERSON_DETECT_INTERVAL_FRAMES below for how that extra cost is kept off
# the live pipeline's CPU budget.
PERSON_DETECT_IMGSZ = int(os.environ.get("PERSON_DETECT_IMGSZ", "960"))

# Independent of SAMPLE_FPS (which still throttles the face-training path
# unchanged): only every Nth processed frame actually runs the (now
# heavier, due to PERSON_DETECT_IMGSZ) person detector. Frames in between
# reuse the last known per-track bbox via self.person_tracks/
# self._live_detections (already how a track survives a momentary miss —
# see PERSON_TRACK_MAX_AGE), so boxes don't disappear or freeze, they just
# update at ~1.5Hz instead of ~3Hz. At 2 gives roughly the SAME total
# person-detection CPU budget as the old 640/every-frame config despite the
# heavier per-call cost — measured, not assumed (see the CPU report this
# change shipped with).
PERSON_DETECT_INTERVAL_FRAMES = int(os.environ.get("PERSON_DETECT_INTERVAL_FRAMES", "2"))

# Independent second safety net against tiny spurious boxes, deliberately
# NOT implemented by raising the confidence threshold back up (that would
# undo the fix above) — any detection below this raw pixel area is dropped
# regardless of confidence. Set well below any real (even distant/seated)
# person's box in these cameras' framing, so it only ever catches genuine
# noise-level detections.
MIN_PERSON_BOX_AREA = float(os.environ.get("PERSON_MIN_BOX_AREA", "3000"))

# --- Rescue detector: a second, independent pass with a stronger (and
# ~3.3x costlier per call, measured) model, run far less often than the
# fast yolov8n pass above. Real-camera testing found frames where yolov8n
# still missed people even at PERSON_DETECT_IMGSZ/lowered confidence —
# specifically people very close to the camera, cropped by the frame edge,
# or at a steep angle (yolov8s found 3/3 real people on a frame where
# yolov8n found only 1/3, confirmed visually, no false positive). Running
# yolov8s at the fast pass's cadence would cost too much CPU on this box;
# instead it only ever ADDS a track the fast pass is missing — it never
# touches or replaces a track the fast pass already has, so normal
# walking/tracking responsiveness is entirely driven by yolov8n, unchanged.
PERSON_RESCUE_WEIGHTS = os.environ.get("PERSON_RESCUE_WEIGHTS", str(_MODELS_DIR / "yolov8s.pt"))

# In raw frame_idx units (same units as PERSON_DETECT_INTERVAL_FRAMES), not
# calls-to-_update_person_overlay units. Must be a multiple of
# PERSON_DETECT_INTERVAL_FRAMES. Measured at ~1.2s/call on this box, so at
# 6 (a sweep every ~2s at SAMPLE_FPS=3) it was costing ~0.6 CPU-core-
# seconds per second PER CAMERA — with three cameras running that is
# roughly 1.8 of this machine's 8 cores spent on the rescue model alone,
# on a box already sitting at 100% CPU. That starvation is what held live
# video down to ~1.8fps against an 8fps target and slowed every other
# pass, including recognition.
#
# 12 halves that cost (a sweep every ~4s) and buys back the headroom that
# makes the video smoother AND pays for the faster classify cadence above.
# The tradeoff is bounded and only affects people the FAST pass misses
# entirely — typically someone seated/back-facing and motionless — who now
# get picked up within ~4s instead of ~2s. Normal walking detection and
# tracking are driven entirely by the fast yolov8n pass and are unchanged.
PERSON_RESCUE_INTERVAL_FRAMES = int(os.environ.get("PERSON_RESCUE_INTERVAL_FRAMES", "12"))

# A rescue-pass box counts as "already found" (and is dropped, not added
# as a duplicate track) if it overlaps an already-tracked person's current
# bbox by at least this IoU — deliberately lower than the person tracker's
# own PERSON_TRACKER_MATCH_THRESHOLD since this is just a coarse
# same-person-or-not check, not the tracker's actual association step.
PERSON_RESCUE_DEDUP_IOU = float(os.environ.get("PERSON_RESCUE_DEDUP_IOU", "0.3"))

# Longer than the face tracker's TRACK_MAX_AGE (30): a person's BODY box is
# meant to survive a longer occlusion/turn-away/face-unavailable stretch
# than a face track would — that's the whole point of being person-first
# rather than face-first. ~15s at SAMPLE_FPS=3.
PERSON_TRACK_MAX_AGE = int(os.environ.get("PERSON_TRACK_MAX_AGE", "45"))

# That limit counts PROCESSED frames, so on a CPU-bound machine (one pass per
# second or slower) 45 of them is a minute or more: boxes of people who have
# long left kept hanging in the picture. A person's box is also dropped once
# they have not been detected for this many real seconds, whichever comes first.
PERSON_TRACK_MAX_SECONDS = float(os.environ.get("PERSON_TRACK_MAX_SECONDS", "6"))

# ByteTrack's own IoU threshold for matching a detection to an existing
# track frame-to-frame (separate from YOLO's detection-time NMS, which was
# checked and confirmed NOT dropping boxes — see PERSON_DEBUG logging
# below). Lowered from ByteTrack's 0.8 default: a live-diagnostic capture
# of two people naturally standing close together showed
# raw_yolo_boxes=2 collapse to after_bytetrack_tracks=1 for one frame
# before self-correcting — a newly-appearing second person close to an
# existing track needed an extra frame to get promoted to its own track at
# 0.8. 0.6 gives the tracker more room to keep two close, distinct boxes
# as two tracks immediately instead of briefly folding one into the other.
PERSON_TRACKER_MATCH_THRESHOLD = float(os.environ.get("PERSON_TRACKER_MATCH_THRESHOLD", "0.6"))

# When ByteTrack fails to match a moving person to their existing track
# (their box moved further than PERSON_TRACKER_MATCH_THRESHOLD allows
# between the infrequent PERSON_DETECT_INTERVAL_FRAMES samples), it hands
# them a brand-new tracker_id rather than losing them — but the OLD
# tracker_id's PersonTrackState doesn't know that happened, so it keeps
# showing its last-known (now stale) box for the rest of PERSON_TRACK_MAX_AGE
# (~15s) alongside the new track's box: two boxes trailing one moving
# person. If a just-appeared new track's box overlaps an existing but
# no-longer-matched ("coasting") track's last-known box by at least this
# IoU, _update_person_overlay treats it as that same handoff and drops the
# old track immediately instead of waiting out its grace period. Two
# genuinely distinct people never trigger this: both would be matched
# (i.e. NOT coasting) in the same frame, since real ByteTrack overlap
# between two different simultaneous people needing this exact handoff
# path is not the failure mode this covers.
PERSON_TRACK_HANDOFF_IOU = float(os.environ.get("PERSON_TRACK_HANDOFF_IOU", "0.4"))

# How often a track's facial expression is re-classified, in the same
# frame_idx units as LIVE_CLASSIFY_INTERVAL_FRAMES. Deliberately much less
# frequent than identity: a mood changes on human timescales, and this is
# an extra model on a CPU-bound box, so it runs roughly every ~2s per track
# instead of every ~0.67s. Identity recognition keeps its own cadence
# untouched — expression only ever piggybacks on a crop that was already
# computed for the embedding.
EXPRESSION_INTERVAL_FRAMES = int(os.environ.get("EXPRESSION_INTERVAL_FRAMES", "6"))

# How long a committed identity stays attached to its person track after
# face recognition last confirmed it — the temporal grace period that stops
# a name flickering off the instant a face turns away or gets briefly
# occluded.
#
# Raised from 8s after measuring what actually reaches the overlay: with
# several people in frame, each recognised person held their name for only
# 1-9% of a 70s window, and names kept vanishing while that same person was
# still being tracked. The cause is not a cap on how many people can be
# named (there is none anywhere) — it is that a confident identity only
# commits when a face is both detected AND classified above
# CLASSIFIER_MIN_PROBA twice in a row, which at this camera distance
# happens in bursts. Between bursts an 8s window expired, so names dropped
# and reappeared and rarely coexisted.
#
# The identity belongs to the TRACK, and the track staying alive is
# ByteTrack asserting it is still the same body, so holding the last
# confidently-established identity for as long as that track lives is the
# correct behaviour — PERSON_TRACK_MAX_AGE already bounds how long a track
# survives without being re-detected, and PERSON_TRACK_HANDOFF_IOU retires
# a track the moment a new one supersedes it. This stays a bounded value
# rather than "forever" so a track that somehow drifts cannot keep a stale
# name indefinitely.
# A name belongs to the person TRACK, not to the last frame a face was readable:
# someone who turns their back, bends over a desk or is hidden behind a monitor
# keeps their name for as long as the track lives (PERSON_TRACK_MAX_SECONDS ends
# it when they really leave). This is only an upper bound for a track that
# somehow never ends. Was 30 s, which turned a recognised person back into
# "Person" half a minute after their face last faced the camera.
IDENTITY_GRACE_SECONDS = float(os.environ.get("IDENTITY_GRACE_SECONDS", "600"))

# Overlay boxes are smoothed (exponential average towards each new detection),
# and a person who is between detections is moved on along their recent
# velocity for at most PERSON_PREDICT_MAX_SECONDS (damped), so boxes glide
# instead of jumping or freezing while the detector is busy.
# A box is dropped PERSON_TRACK_MAX_SECONDS after the person was last detected - or,
# when this machine is slow and the detector only comes round every few seconds,
# 2.5 detector cycles later (never more than PERSON_TRACK_MAX_HOLD_SECONDS), so a
# slow cycle does not look like the person leaving.
PERSON_TRACK_MAX_HOLD_SECONDS = float(os.environ.get("PERSON_TRACK_MAX_HOLD_SECONDS", "15"))
# Face detections are produced by their own worker; the person loop uses the latest
# ones if they are at most this old (and crops the face from the frame it was found in).
FACE_RESULT_MAX_AGE = float(os.environ.get("FACE_RESULT_MAX_AGE", "4"))
PERSON_BOX_SMOOTHING = float(os.environ.get("PERSON_BOX_SMOOTHING", "0.6"))
PERSON_PREDICT_MAX_SECONDS = float(os.environ.get("PERSON_PREDICT_MAX_SECONDS", "1.0"))

# The slow rescue model runs in its own thread; its result describes the frame it
# was given, so it is only used if it is still this recent when it finishes.
PERSON_RESCUE_RESULT_MAX_AGE = float(os.environ.get("PERSON_RESCUE_RESULT_MAX_AGE", "5"))

# Face recognition work waiting for the recognition worker (per camera). Full
# queue = the newest work for that person is skipped, never accumulated.
RECOGNITION_QUEUE_SIZE = int(os.environ.get("FACE_RECOGNITION_QUEUE_SIZE", "6"))

# How long a person's face box is kept on the live viewer after the face was last found.
FACE_BOX_HOLD_SECONDS = float(os.environ.get("FACE_BOX_HOLD_SECONDS", "1.5"))

# Consecutive confident classifier reads of the SAME candidate identity
# required before a person track's displayed identity is committed or
# switched — one lucky (or unlucky) frame is not enough to change what's
# shown, which is what caused the old face-track overlay to flicker.
RECOGNITION_STABILITY_FRAMES = int(os.environ.get("RECOGNITION_STABILITY_FRAMES", "2"))

# A single classifier read at or above this confidence commits the identity
# immediately, skipping the RECOGNITION_STABILITY_FRAMES vote — so a clearly
# visible face is named on the first frame it's seen. Measured on held-out
# validation data: reads >= 0.9 are wrong ~0.3% of the time, about the same
# as the two-vote path. Reads between CLASSIFIER_MIN_PROBA and this still
# need the vote.
INSTANT_COMMIT_PROBA = float(os.environ.get("INSTANT_COMMIT_PROBA", "0.9"))

# The same shortcut for an enrolled-gallery match (cosine similarity, not a
# probability): one read this strong and this far ahead of the next person names
# the track at once. Measured on this install's labelled CCTV captures, the best
# match to a DIFFERENT person stayed under ~0.29 (p95), so 0.5 with a 0.15 lead is
# far outside what a wrong person scores. Each recognition costs seconds on this
# CPU; needing two reads per person was what left most people of a busy room
# unnamed. Weaker matches still need the RECOGNITION_STABILITY_FRAMES vote.
INSTANT_COMMIT_SIMILARITY = float(os.environ.get("INSTANT_COMMIT_SIMILARITY", "0.5"))
INSTANT_COMMIT_MARGIN = float(os.environ.get("INSTANT_COMMIT_MARGIN", "0.15"))

# Detector input size when re-finding the face inside a small face crop (live
# recognition). The full 640x640 is meant for whole photos; a crop is ~100 px and
# mostly face, so a smaller input finds the same face and landmarks for a
# fraction of the work (measured, see the report). Enrolment photos keep 640.
FACE_EMBED_DET_SIZE = int(os.environ.get("FACE_EMBED_DET_SIZE", "256"))

# Identity hand-off by body appearance (appearance.py): a person with no
# face identity is compared, at most every APPEARANCE_INTERVAL_SECONDS and for
# at most APPEARANCE_MATCHES_PER_CYCLE people per update, against people face-
# recognised today; APPEARANCE_STABILITY agreeing matches in a row are needed
# before the name is shown.
APPEARANCE_INTERVAL_SECONDS = float(os.environ.get("APPEARANCE_INTERVAL_SECONDS", "2.0"))
APPEARANCE_MATCHES_PER_CYCLE = 2
APPEARANCE_STABILITY = 2

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(TRAINING_UNLABELED_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Track state (per camera)
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    """Face track — feeds the training-capture/review pipeline ONLY
    (_recognize_and_route / _save_training_capture). The live-view overlay
    no longer reads anything from this; see PersonTrackState below."""
    track_id: int
    best_score: float = -1.0   # proxy for "quality" — we use bbox area * detection conf
    best_conf: float = -1.0    # raw detection confidence at the best_score frame
    best_area: float = 0.0     # raw (tight, unpadded) YOLO bbox area at the best_score frame
    best_crop: np.ndarray | None = None
    last_seen_frame: int = 0
    pushed_to_review: bool = False


@dataclass
class PersonTrackState:
    """Person track — drives the live-view overlay. A person's existence
    here depends only on the person detector/tracker, never on whether a
    face is currently visible (see _update_person_overlay). Identity is a
    separate, temporally-stabilized layer on top: current_identity only
    changes after RECOGNITION_STABILITY_FRAMES consecutive confident reads
    of the same candidate, and stays displayed for IDENTITY_GRACE_SECONDS
    after last confirmed even if the face becomes unavailable again."""
    track_id: int
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    last_seen_frame: int = 0
    last_seen_time: float = 0.0           # wall clock of the last real detection of this person
    current_identity: str | None = None   # committed employee_id, or None -> "Person"
    identity_confidence: float = 0.0
    last_recognized_time: float = 0.0
    pending_identity: str | None = None   # stability-vote candidate, not yet committed
    pending_count: int = 0
    last_classify_frame: int = -999_999
    last_embed_retry: float = 0.0   # wall clock of the last bordered embedding retry
    # Expression is advisory metadata only — it is never read by any
    # identity decision above, so a missing or failed mood can't change
    # who this person is (see expression.py).
    last_expression_frame: int = -999_999
    # Identity from body appearance (appearance.py), used only while there's
    # no face identity to show. Same hold as a face identity.
    appearance_identity: str | None = None
    appearance_confidence: float = 0.0
    appearance_time: float = 0.0
    appearance_pending: str | None = None
    appearance_pending_count: int = 0
    appearance_last_try: float = 0.0
    # Where this person's face was last found (full-frame pixels), for the
    # live viewer's face boxes. Held briefly so the box doesn't flicker.
    face_bbox: tuple[int, int, int, int] | None = None
    face_time: float = 0.0
    # Smoothed box + centre velocity (px/s) used for display; bbox above stays
    # the raw detection that face association and appearance use.
    disp_box: tuple[float, float, float, float] | None = None
    vel: tuple[float, float] = (0.0, 0.0)
    first_seen_time: float = 0.0
    # A recognition job for this person is already queued/running.
    recog_inflight: bool = False


class CameraFacePipeline:
    """One instance per camera. Call feed_frame(frame) from the same loop
    that already reads RTSP frames in camera_stream.py — no separate
    connection to the camera is needed."""

    _yolo = None
    _yolo_person = None
    _yolo_person_rescue = None
    _arcface = None
    _model_lock = threading.Lock()
    _classifier = None        # sklearn classifier, lazy-loaded from CLASSIFIER_PATH
    _classifier_mtime = None  # reload automatically if the file changes (retrained)

    def __init__(self, camera_id: int):
        self.camera_id = camera_id
        self.tracks: dict[int, TrackState] = {}
        self.person_tracks: dict[int, PersonTrackState] = {}
        self.frame_idx = 0
        self._last_sample_time = 0.0
        # Negative sentinel so the very first has_viewer frame always runs
        # detection immediately rather than waiting a full
        # PERSON_DETECT_INTERVAL_FRAMES after this pipeline is created.
        self._last_person_detect_frame = -PERSON_DETECT_INTERVAL_FRAMES
        self._last_rescue_frame = -PERSON_RESCUE_INTERVAL_FRAMES
        # Recognition (embedding + match) and the slow rescue detector run on
        # their own bounded workers so a busy face or a heavy sweep can never
        # stall the person cycle that keeps boxes alive. See close().
        self._recog_queue: queue.Queue = queue.Queue(maxsize=RECOGNITION_QUEUE_SIZE)
        self._recog_thread = threading.Thread(
            target=self._recog_worker, name=f"recog-cam{camera_id}", daemon=True)
        self._recog_thread.start()
        self._rescue_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"rescue-cam{camera_id}")
        self._rescue_future = None
        self.stage_ms: dict[str, float] = {}
        self._cycle_s = 0.0
        # Face detection + face tracking + dataset capture run on their own worker:
        # feed_frame() hands over the newest frame and carries on with the people.
        self._face_slot = None
        self._face_event = threading.Event()
        self._face_closed = False
        self._face_error: Exception | None = None
        self._faces_latest = None            # (detections, time, frame they came from)
        self._face_thread = threading.Thread(target=self._face_worker, name=f"faces-cam{camera_id}", daemon=True)
        self._face_thread.start()
        self._gallery_cache: list[dict] = []
        self._gallery_loaded_at = 0.0
        self._gallery_index = face_matching.build_index([])
        self._gallery_problems_logged: tuple = (0, 0, 0)
        # Diagnostics (see _diag): last log time per reason, running counts,
        # and the last overlay MatchResult for the failure line.
        self._diag_last_log: dict[str, float] = {}
        self._diag_counts: dict[str, int] = {}
        self._last_match: face_matching.MatchResult | None = None
        # Recent (timestamp, embedding) pairs for THIS camera only, used by
        # _is_recent_duplicate() — only ever read/written by this camera's
        # own background thread (feed_frame is called from exactly one
        # thread per camera, see camera_stream.py), so no lock needed here.
        self._recent_captures: list[tuple[float, np.ndarray]] = []
        # Rebuilt in full at the end of every feed_frame() call (see
        # get_live_detections) — read from the websocket-serving asyncio
        # loop in main.py, written from this camera's own single background
        # thread. A plain list-reference swap is what's read; no lock, same
        # reasoning as _recent_captures above (single writer thread) plus
        # CPython list/attribute assignment being atomic under the GIL.
        self._live_detections: list[dict] = []
        self.frame_size: tuple[int, int] | None = None  # (width, height) of frames fed in
        # Freshness of the live overlay, so the viewer can say "recognition delayed".
        self.last_overlay_at: float = 0.0
        self.last_face_at: float = 0.0
        self.process_ms: float | None = None  # smoothed cost of one feed_frame() pass
        self._ensure_models_loaded()

        # ByteTrack: lightweight, no GPU needed, just IoU + Kalman motion.
        # `supervision`'s implementation (see module docstring for why, over
        # the original `yolox` package). Two independent instances/track-id
        # spaces — face tracks (training-capture pipeline, unchanged) and
        # person tracks (live overlay) are tracked separately since they're
        # different detections with different lifetimes.
        self.tracker = sv.ByteTrack(
            track_activation_threshold=0.5,
            lost_track_buffer=TRACK_MAX_AGE,
            minimum_matching_threshold=0.8,
        )
        self.person_tracker = sv.ByteTrack(
            track_activation_threshold=0.4,
            lost_track_buffer=PERSON_TRACK_MAX_AGE,
            minimum_matching_threshold=PERSON_TRACKER_MATCH_THRESHOLD,
        )

    @classmethod
    def _ensure_arcface_loaded(cls):
        """Only the embedding model — enough for enrolling a photo directly
        (face_routes.enroll), which doesn't need detection/tracking at all
        since the human already framed the face."""
        with cls._model_lock:
            if cls._arcface is None:
                # This box has no CUDA (torch reports cuda.is_available() ==
                # False, and only plain `onnxruntime` is installed, not
                # `onnxruntime-gpu`) — CPUExecutionProvider only. Swap back
                # to the CUDA+CPU list if this ever runs on a GPU box.
                # Only detection (5-point landmarks for alignment) and
                # recognition (the embedding) — buffalo_l otherwise also
                # runs genderage + two dense-landmark models on every face,
                # none of which anything here reads. Measured: identical
                # embeddings (cosine >= 0.99999), ~2.6x faster per face.
                # Explicit root + offline check: InsightFace otherwise downloads
                # buffalo_l from GitHub on first use (see models.py).
                from app import config as _config, models as _models

                _models.require("insightface_buffalo_l")
                cls._arcface = insightface.app.FaceAnalysis(
                    name="buffalo_l", root=str(_config.INSIGHTFACE_ROOT), providers=["CPUExecutionProvider"],
                    allowed_modules=["detection", "recognition"],
                )
                cls._arcface.prepare(ctx_id=-1, det_size=(640, 640))

    @classmethod
    def _ensure_yolo_loaded(cls):
        with cls._model_lock:
            if cls._yolo is None:
                from app import models as _models

                cls._yolo = YOLO(str(_models.require("yolo_face")))

    @classmethod
    def _ensure_yolo_person_loaded(cls):
        """Generic COCO detector for the person-first live overlay — a
        separate model instance from the face-tuned one above (see module
        docstring for why the face model is never reused for this)."""
        with cls._model_lock:
            if cls._yolo_person is None:
                from app import models as _models

                cls._yolo_person = YOLO(str(_models.require("yolo_person")))

    @classmethod
    def _ensure_yolo_person_rescue_loaded(cls):
        """Stronger, slower model used only for the occasional rescue sweep
        — see PERSON_RESCUE_WEIGHTS above. Loaded eagerly alongside the
        other models (same pattern as _ensure_yolo_person_loaded) rather
        than on first use, so a rescue cycle never pays a cold-load stall
        the first time it fires."""
        with cls._model_lock:
            if cls._yolo_person_rescue is None:
                from app import models as _models

                cls._yolo_person_rescue = YOLO(str(_models.require("yolo_person_rescue")))

    @classmethod
    def _ensure_models_loaded(cls):
        """All four models — needed for the live per-camera pipeline
        (face detect + track + embed, plus person detect + rescue for the
        overlay), unlike enrollment which only needs ArcFace."""
        first = cls._arcface is None
        cls._ensure_yolo_loaded()
        cls._ensure_yolo_person_loaded()
        cls._ensure_yolo_person_rescue_loaded()
        cls._ensure_arcface_loaded()
        if first:
            # The models are loaded for the rest of the run: keep the garbage
            # collector from re-scanning them (see heap.py).
            from app import heap

            heap.freeze("face models loaded")

    def _refresh_gallery(self):
        # Reload the enrolled embeddings every 30s rather than per-frame.
        # Interval-only, deliberately not "...or the cache is empty": an
        # empty gallery is a normal state (nobody enrolled yet), and the
        # live overlay path now consults this too, so re-querying whenever
        # it's empty would mean a DB round-trip per recognition cycle per
        # camera forever.
        now = time.time()
        if now - self._gallery_loaded_at > 30:
            self._gallery_cache = face_db.get_all_embeddings()
            self._gallery_loaded_at = now
            # The live overlay matches against a de-duplicated, conflict-checked
            # view of the gallery (face_matching.build_index). DB untouched.
            self._gallery_index = face_matching.build_index(self._gallery_cache)
            problems = (self._gallery_index.duplicates_dropped, self._gallery_index.invalid_dropped,
                        len(self._gallery_index.conflicts))
            if any(problems) and problems != self._gallery_problems_logged:
                self._gallery_problems_logged = problems
                log.warning(
                    "camera %s: enrolled gallery has %d duplicate reference(s) ignored, %d invalid, "
                    "%d cross-person conflict(s) %s excluded from matching - fix the enrollment data",
                    self.camera_id, *problems,
                    [(a, b) for a, b, _ in self._gallery_index.conflicts])

    @classmethod
    def _get_classifier(cls):
        """Lazy-load the trained classifier and pick up a fresh one
        automatically if POST /api/faces/training/train produced a new file
        while this process is running — checked via mtime, not re-imported
        from disk on every single match (that stat call is cheap; loading
        the pickle is not)."""
        if not os.path.exists(CLASSIFIER_PATH):
            cls._classifier, cls._classifier_mtime = None, None
            return None
        mtime = os.path.getmtime(CLASSIFIER_PATH)
        if mtime != cls._classifier_mtime:
            with cls._model_lock:
                if mtime != cls._classifier_mtime:
                    # Verified load (classifier_io): a model that can't run
                    # under this scikit-learn returns None -> gallery-only
                    # matching, and is not retried until the file changes.
                    from app import classifier_io

                    cls._classifier = classifier_io.load_verified(CLASSIFIER_PATH)
                    cls._classifier_mtime = mtime
        return cls._classifier

    @staticmethod
    def _pad_bbox(x1: int, y1: int, x2: int, y2: int, frame_shape: tuple, margin: float = FACE_CROP_MARGIN):
        """Expand a detection box by `margin` fraction of its size on each
        side, clamped to the frame. See FACE_CROP_MARGIN above for why this
        exists — feeds InsightFace's re-detection/alignment step (in
        _embed(), unchanged) enough surrounding context to actually find
        the face, instead of a bbox-tight crop it reliably fails on."""
        h, w = frame_shape[:2]
        bw, bh = x2 - x1, y2 - y1
        mx, my = int(bw * margin), int(bh * margin)
        return max(0, x1 - mx), max(0, y1 - my), min(w, x2 + mx), min(h, y2 + my)

    @staticmethod
    def _quality_metrics(crop: np.ndarray) -> tuple[float, float]:
        """Cheap, first-version-only quality signals: blur (variance of
        Laplacian — higher is sharper) and brightness (mean grayscale
        intensity). Stored alongside every capture so a future pass can
        filter out unusable samples before training without having to
        re-derive this from the image again."""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())
        return blur, brightness

    def feed_frame(self, frame: np.ndarray, has_viewer: bool = True):
        """Call this on every frame from the existing RTSP read loop.
        Internally throttles to SAMPLE_FPS so we don't run YOLO on every
        single frame — 2-5 fps is plenty for catching faces walking past.

        `has_viewer`: whether anyone is actually watching this camera's
        live feed right now (see camera_stream.CameraStream.has_real_viewer).
        The background face-collection dataset pipeline below ALWAYS runs
        regardless — that's real training data being built 24/7 and must
        not depend on a browser tab being open. Only the person-detection
        live-overlay (a second full YOLO pass, purely cosmetic for a
        connected viewer) is skipped while has_viewer is False, so it isn't
        burning CPU around the clock for a display nobody is looking at."""
        now = time.time()
        self.frame_size = (frame.shape[1], frame.shape[0])
        if now - self._last_sample_time < (1.0 / SAMPLE_FPS):
            return
        self._last_sample_time = now
        self.frame_idx += 1

        # Face detection / face tracking / dataset capture happen on the face worker
        # (_process_faces); this thread only hands it the newest frame and moves on
        # to the people, which must never wait for a face. Without a worker (tests)
        # the same work runs inline.
        err = self.__dict__.get("_face_error")
        if err is not None:
            self._face_error = None
            raise err                      # surfaces in camera_stream's face_health (pause / rebuild)
        if self.__dict__.get("_face_thread") is None:
            detections, face_frame = self._process_faces(frame), frame
        else:
            self._face_slot = frame        # latest frame wins; nothing queues up
            self._face_event.set()
            detections, face_frame = self._recent_faces(frame)

        # --- Person-first live overlay: entirely separate track space and
        # display logic. Reuses this same frame's face detections (above)
        # for identity, but a person's box/existence never depends on one
        # being found. Skipped while nobody is watching — see has_viewer
        # docstring above. person_tracks is left as-is (not cleared) while
        # skipped; frame_idx keeps advancing underneath it, so on the next
        # real frame every existing track reads as long-stale and is
        # dropped cleanly rather than reappearing with a stale box. ---
        if has_viewer and self.frame_idx - self._last_person_detect_frame >= PERSON_DETECT_INTERVAL_FRAMES:
            self._last_person_detect_frame = self.frame_idx
            self._update_person_overlay(frame, detections, face_frame)

    def _recent_faces(self, frame: np.ndarray):
        """Latest face detections (and the frame they came from) if still fresh."""
        latest = self.__dict__.get("_faces_latest")
        if latest is None or time.time() - latest[1] > FACE_RESULT_MAX_AGE:
            return np.empty((0, 5), dtype=np.float32), frame
        return latest[0], latest[2]

    def _face_worker(self) -> None:
        while True:
            self._face_event.wait()
            self._face_event.clear()
            if self._face_closed:
                return
            frame, self._face_slot = self._face_slot, None
            if frame is None:
                continue
            started = time.perf_counter()
            try:
                dets = self._process_faces(frame)
                self._faces_latest = (dets, time.time(), frame)
            except Exception as e:                 # reported by the next feed_frame()
                log.exception("camera %s: face worker failed", self.camera_id)
                self._face_error = e
            finally:
                self._tick("face_cycle", started)

    def _process_faces(self, frame: np.ndarray) -> np.ndarray:
        """Face detection, face tracking and the training-capture/review flow for
        one frame (unchanged logic, moved off the person loop). Returns the raw
        face detections, which the person overlay uses to find faces inside people."""
        # --- Face detection + face tracking: UNCHANGED from before the
        # person-first overlay existed. Feeds ONLY the training-capture /
        # review pipeline below (_recognize_and_route / _save_training_capture)
        # — the live-view overlay no longer reads from this at all (see
        # _update_person_overlay). ---
        _t = time.perf_counter()
        detections = self._detect_faces(frame)  # -> np.array [[x1,y1,x2,y2,conf], ...]
        self._tick("face_detect", _t)
        if len(detections):
            sv_detections = sv.Detections(xyxy=detections[:, :4], confidence=detections[:, 4])
            tracked = self.tracker.update_with_detections(sv_detections)
        else:
            tracked = sv.Detections.empty()

        for i in range(len(tracked)):
            x1, y1, x2, y2 = tracked.xyxy[i]
            track_id = int(tracked.tracker_id[i])
            score = float(tracked.confidence[i]) if tracked.confidence is not None else 1.0

            x1, y1, x2, y2 = map(lambda v: max(0, int(v)), (x1, y1, x2, y2))
            px1, py1, px2, py2 = self._pad_bbox(x1, y1, x2, y2, frame.shape)
            crop = frame[py1:py2, px1:px2]
            if crop.size == 0:
                continue

            # Quality/tracking scoring stays keyed on the tight YOLO box —
            # only the stored crop itself is padded.
            area = (x2 - x1) * (y2 - y1)
            quality = area * score

            state = self.tracks.setdefault(track_id, TrackState(track_id=track_id))
            state.last_seen_frame = self.frame_idx
            if quality > state.best_score:
                state.best_score = quality
                state.best_conf = score
                state.best_area = area
                state.best_crop = crop.copy()

        # Desk analytics (desks.py): identifies faces sitting inside a drawn
        # desk outline, about once a second. Called even with no faces so
        # the tracker can mark people away once their grace period lapses.
        if desks.service.has_zones(self.camera_id):
            desks.service.observe(
                self.camera_id, frame.shape,
                [(int(tracked.tracker_id[i]), [float(v) for v in tracked.xyxy[i]]) for i in range(len(tracked))],
                lambda bbox: self._identify(frame, bbox),
            )

        # Visitor Management (visitors.py): while someone in a visitor area is
        # not yet known to be an employee, the faces inside their body box are
        # identified here, so a seated employee is recognised without waiting
        # for their face track to end and without anyone watching the overlay.
        # Rate-limited and a no-op on cameras without such a person.
        from app import visitors

        if visitors.service.wants_faces(self.camera_id):
            visitors.service.observe_faces(
                self.camera_id, frame.shape,
                [[float(v) for v in tracked.xyxy[i]] for i in range(len(tracked))],
                lambda bbox: self._identify_face(frame, bbox),
            )

        # Drop stale tracks and flush any that finished (left frame / went stale)
        # to recognition — this is where "best frame per track" pays off:
        # one embedding computed per person-appearance, not per frame.
        stale = [
            tid for tid, s in self.tracks.items()
            if self.frame_idx - s.last_seen_frame > TRACK_MAX_AGE
        ]
        for tid in stale:
            state = self.tracks.pop(tid)
            if state.best_crop is not None and not state.pushed_to_review:
                self._recognize_and_route(state)

        return detections

    def _detect_faces(self, frame: np.ndarray) -> np.ndarray:
        results = self._yolo.predict(frame, verbose=False, conf=0.5)[0]
        if results.boxes is None or len(results.boxes) == 0:
            return np.empty((0, 5), dtype=np.float32)
        boxes = results.boxes.xyxy.cpu().numpy()
        confs = results.boxes.conf.cpu().numpy()
        return np.hstack([boxes, confs[:, None]]).astype(np.float32)

    @staticmethod
    def _filter_person_boxes(results) -> np.ndarray:
        """Shared by the fast and rescue person detectors: same contract as
        _detect_faces's return shape, plus MIN_PERSON_BOX_AREA — a second,
        confidence-independent floor against noise-level boxes."""
        if results.boxes is None or len(results.boxes) == 0:
            return np.empty((0, 5), dtype=np.float32)
        boxes = results.boxes.xyxy.cpu().numpy()
        confs = results.boxes.conf.cpu().numpy()
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        keep = areas >= MIN_PERSON_BOX_AREA
        return np.hstack([boxes[keep], confs[keep, None]]).astype(np.float32)

    def _detect_persons(self, frame: np.ndarray) -> np.ndarray:
        """The fast, frequent pass (every PERSON_DETECT_INTERVAL_FRAMES) —
        generic COCO model restricted to class 0 ("person") — classes=[0]
        so a chair or laptop never becomes a detection in the first place,
        rather than being filtered out after the fact. imgsz=
        PERSON_DETECT_IMGSZ (see its comment) is what recovers a
        seated/back-facing person that the library's 640 default missed
        entirely. Drives all normal tracking/walking responsiveness."""
        results = self._yolo_person.predict(
            frame, verbose=False, conf=PERSON_DETECT_CONF, classes=[0], imgsz=PERSON_DETECT_IMGSZ,
        )[0]
        return self._filter_person_boxes(results)

    def _detect_persons_rescue(self, frame: np.ndarray) -> np.ndarray:
        """The slow, occasional pass (every PERSON_RESCUE_INTERVAL_FRAMES)
        — a stronger, costlier model, same conf/imgsz/area filtering, used
        only to catch people the fast pass above is missing (see
        PERSON_RESCUE_WEIGHTS). Never used to update a track the fast pass
        already found — see _merge_rescue_detections."""
        results = self._yolo_person_rescue.predict(
            frame, verbose=False, conf=PERSON_DETECT_CONF, classes=[0], imgsz=PERSON_DETECT_IMGSZ,
        )[0]
        return self._filter_person_boxes(results)

    def _run_rescue(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        started = time.perf_counter()
        t_frame = time.time()
        dets = self._detect_persons_rescue(frame)
        self._tick("rescue", started)
        return dets, t_frame

    def _collect_rescue(self, frame: np.ndarray) -> np.ndarray:
        """Boxes from the slow rescue model for this cycle, never waiting for it.

        The sweep runs on its own thread (one at a time); a finished result is
        used if it is still recent, and boxes that overlap a person already on
        screen are dropped so a late result cannot duplicate or move anybody.
        A new sweep starts when one is due and none is running. Without a
        worker (tests) it runs inline as it used to."""
        empty = np.empty((0, 5), dtype=np.float32)
        pool = self.__dict__.get("_rescue_pool")
        due = self.frame_idx - self._last_rescue_frame >= PERSON_RESCUE_INTERVAL_FRAMES
        if pool is None:
            if not due:
                return empty
            self._last_rescue_frame = self.frame_idx
            return self._detect_persons_rescue(frame)
        out = empty
        fut = self.__dict__.get("_rescue_future")
        if fut is not None and fut.done():
            self._rescue_future = None
            try:
                dets, t_frame = fut.result()
            except Exception:
                log.exception("camera %s: rescue person sweep failed", self.camera_id)
                dets, t_frame = empty, 0.0
            if len(dets) and time.time() - t_frame <= PERSON_RESCUE_RESULT_MAX_AGE:
                shown = [np.array(p.bbox, dtype=np.float32) for p in self.person_tracks.values()
                         if time.time() - p.last_seen_time <= self._track_hold()]
                keep = [d for d in dets
                        if not shown or max(self._iou(d, b) for b in shown) < PERSON_RESCUE_DEDUP_IOU]
                if keep:
                    out = np.array(keep, dtype=np.float32)
        if self.__dict__.get("_rescue_future") is None and due:
            self._last_rescue_frame = self.frame_idx
            self._rescue_future = pool.submit(self._run_rescue, frame)
        return out

    @staticmethod
    def _person_tracker_scores(conf: np.ndarray) -> np.ndarray:
        """Scores handed to the person ByteTrack, not the raw detector scores.

        ByteTrack only STARTS a track for a detection scoring above its
        track_activation_threshold + 0.1 (0.5 here), so a person the detector
        reports at 0.3-0.5 - exactly the seated / half-hidden people
        PERSON_DETECT_CONF was lowered to catch - was accepted by the detector
        and then silently never shown (tested: 0.45 -> no track, 0.51 -> track;
        and lowering the threshold alone only makes such tracks flicker). The
        detector has already applied PERSON_DETECT_CONF and MIN_PERSON_BOX_AREA,
        so the tracker does not need to re-filter by score: map [PERSON_DETECT_CONF,
        1] monotonically onto [0.6, 1.0] so every accepted detection can be tracked
        and the ordering the tracker uses to match boxes is unchanged."""
        c = np.clip(np.asarray(conf, dtype=np.float32), PERSON_DETECT_CONF, 1.0)
        return 0.6 + (c - PERSON_DETECT_CONF) / max(1.0 - PERSON_DETECT_CONF, 1e-6) * 0.4

    @staticmethod
    def _iou(box_a: np.ndarray, box_b: np.ndarray) -> float:
        ax1, ay1, ax2, ay2 = box_a[:4]
        bx1, by1, bx2, by2 = box_b[:4]
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def _merge_rescue_detections(self, fast_dets: np.ndarray, rescue_dets: np.ndarray) -> np.ndarray:
        """Appends only the rescue-pass boxes that don't already overlap a
        fast-pass box (PERSON_RESCUE_DEDUP_IOU) — this is what makes the
        rescue pass strictly additive: it can only ever introduce a NEW
        track for someone the fast pass missed this cycle, never move,
        replace, or duplicate a box the fast pass already produced."""
        if len(rescue_dets) == 0:
            return fast_dets
        if len(fast_dets) == 0:
            return rescue_dets
        new_boxes = [
            rd for rd in rescue_dets
            if max(self._iou(rd, fd) for fd in fast_dets) < PERSON_RESCUE_DEDUP_IOU
        ]
        if not new_boxes:
            return fast_dets
        return np.vstack([fast_dets, np.array(new_boxes, dtype=np.float32)])

    @staticmethod
    def _faces_by_person(person_boxes: list[tuple[int, int, int, int]], face_detections: np.ndarray) -> dict[int, np.ndarray]:
        """Maps person-box index -> the best face detection whose CENTER
        point falls inside that box (clearest faces claimed first, each
        face used at most once) — this is the "person exists -> look for a
        face inside it" association, never the other way around. A person
        with no face inside simply gets no entry here, which is expected
        and handled by the caller (identity just doesn't update this cycle,
        the box is shown regardless)."""
        assignments: dict[int, np.ndarray] = {}
        if len(face_detections) == 0 or not person_boxes:
            return assignments
        order = sorted(range(len(face_detections)), key=lambda i: -face_detections[i][4])
        for fi in order:
            fx1, fy1, fx2, fy2, _fconf = face_detections[fi]
            cx, cy = (fx1 + fx2) / 2, (fy1 + fy2) / 2
            for pi, (px1, py1, px2, py2) in enumerate(person_boxes):
                if pi in assignments:
                    continue
                if px1 <= cx <= px2 and py1 <= cy <= py2:
                    assignments[pi] = face_detections[fi]
                    break
        return assignments

    def _track_hold(self) -> float:
        """Seconds a person stays on screen after the detector last saw them."""
        return max(PERSON_TRACK_MAX_SECONDS, min(PERSON_TRACK_MAX_HOLD_SECONDS, 2.5 * self.__dict__.get("_cycle_s", 0.0)))

    def _update_person_overlay(self, frame: np.ndarray, face_detections: np.ndarray, face_frame: np.ndarray | None = None) -> None:
        """Person-first: detect + track bodies unconditionally, then for
        each currently-visible person track, try to find a face inside its
        box and (throttled) refresh its temporally-stabilized identity. A
        person's box is shown regardless of whether a face is found — see
        module-level PersonTrackState docstring for the stability/grace
        rules that decide what label goes with it."""
        _t = time.perf_counter()
        person_dets = self._detect_persons(frame)
        self._tick("person_detect", _t)
        person_dets = self._merge_rescue_detections(person_dets, self._collect_rescue(frame))

        # Always call update_with_detections, even with zero detections —
        # ByteTrack still needs every frame to age its internal state
        # correctly. Its RETURN value only ever contains tracks matched to
        # an actual detection THIS frame though (it doesn't Kalman-predict
        # a box forward through a miss), so a single frame where the
        # detector found nobody would otherwise wipe every person off the
        # overlay — directly against "box must not disappear just because
        # detection momentarily missed". Fixed below by building the
        # overlay from self.person_tracks (each track's LAST KNOWN bbox,
        # kept until it's truly gone for PERSON_TRACK_MAX_AGE frames), not
        # from this frame's raw tracker output alone.
        if len(person_dets):
            sv_persons = sv.Detections(xyxy=person_dets[:, :4], confidence=self._person_tracker_scores(person_dets[:, 4]))
            tracked_persons = self.person_tracker.update_with_detections(sv_persons)
        else:
            tracked_persons = self.person_tracker.update_with_detections(sv.Detections.empty())

        n = len(tracked_persons)
        seen_this_frame: dict[int, tuple[int, int, int, int]] = {}
        for i in range(n):
            track_id = int(tracked_persons.tracker_id[i])
            x1, y1, x2, y2 = map(lambda v: max(0, int(v)), tracked_persons.xyxy[i])
            seen_this_frame[track_id] = (x1, y1, x2, y2)

        # Face association only makes sense against boxes actually detected
        # this frame — a coasting track's stale bbox has no fresh face
        # detection to look inside.
        track_ids_this_frame = list(seen_this_frame.keys())
        boxes_this_frame = [seen_this_frame[tid] for tid in track_ids_this_frame]
        face_assignments = self._faces_by_person(boxes_this_frame, face_detections)

        # ID-handoff cleanup — see PERSON_TRACK_HANDOFF_IOU. Only ever
        # compares a track_id ByteTrack did NOT match this frame ("coasting"
        # on its grace period, i.e. not in seen_this_frame) against one that
        # just appeared for the first time; two people simultaneously
        # matched this same frame are never touched here.
        new_track_ids = [tid for tid in track_ids_this_frame if tid not in self.person_tracks]
        handed_over: dict[int, PersonTrackState] = {}
        if new_track_ids:
            coasting_ids = [tid for tid in self.person_tracks if tid not in seen_this_frame]
            for new_tid in new_track_ids:
                new_box = np.array(seen_this_frame[new_tid], dtype=np.float32)
                for old_tid in coasting_ids:
                    if self._iou(new_box, np.array(self.person_tracks[old_tid].bbox, dtype=np.float32)) >= PERSON_TRACK_HANDOFF_IOU:
                        # Same person under a new track id: the old track's name
                        # (and smoothed box) move to the new one instead of being
                        # thrown away and re-earned from scratch.
                        handed_over[new_tid] = self.person_tracks[old_tid]
                        del self.person_tracks[old_tid]
                        coasting_ids.remove(old_tid)
                        break

        for idx, track_id in enumerate(track_ids_this_frame):
            fresh = track_id not in self.person_tracks
            pstate = self.person_tracks.setdefault(track_id, PersonTrackState(track_id=track_id))
            if fresh:
                pstate.first_seen_time = time.time()
                old = handed_over.get(track_id)
                if old is not None:
                    pstate.current_identity = old.current_identity
                    pstate.identity_confidence = old.identity_confidence
                    pstate.last_recognized_time = old.last_recognized_time
                    pstate.appearance_identity = old.appearance_identity
                    pstate.appearance_confidence = old.appearance_confidence
                    pstate.appearance_time = old.appearance_time
                    pstate.face_bbox, pstate.face_time = old.face_bbox, old.face_time
                    pstate.disp_box, pstate.vel = old.disp_box, old.vel
                    pstate.first_seen_time = old.first_seen_time or pstate.first_seen_time
                self._diag_event("track_new", track=track_id, handed_over=old is not None)
            self._smooth_box(pstate, boxes_this_frame[idx], time.time())
            pstate.bbox = boxes_this_frame[idx]
            pstate.last_seen_frame = self.frame_idx
            pstate.last_seen_time = time.time()
            face_row = face_assignments.get(idx)
            if face_row is not None:
                pstate.face_bbox = tuple(int(v) for v in face_row[:4])
                pstate.face_time = time.time()
                self.last_face_at = pstate.face_time
                self._update_person_identity(pstate, face_frame if face_frame is not None else frame, face_row, frame)

        self._update_appearance_identities(frame, track_ids_this_frame)

        now = time.time()
        hold = self._track_hold()
        live_this_frame = []
        stale_persons = []
        for track_id, pstate in self.person_tracks.items():
            if (self.frame_idx - pstate.last_seen_frame > PERSON_TRACK_MAX_AGE
                    or (pstate.last_seen_time and now - pstate.last_seen_time > hold)):
                stale_persons.append(track_id)
                continue

            # Grace period evaluated fresh every frame at display time, not
            # baked into current_identity itself — so it decays smoothly
            # rather than needing its own timer/callback.
            display_identity = None
            identity_source = None
            confidence = 0.0
            if pstate.current_identity and (now - pstate.last_recognized_time) <= IDENTITY_GRACE_SECONDS:
                display_identity, identity_source, confidence = pstate.current_identity, "face", pstate.identity_confidence
            elif pstate.appearance_identity and (now - pstate.appearance_time) <= IDENTITY_GRACE_SECONDS:
                # No face identity (face not visible): named by body appearance
                # matched against someone face-recognised today (appearance.py).
                display_identity, identity_source, confidence = pstate.appearance_identity, "appearance", pstate.appearance_confidence

            # name/color resolved from employee_directory.py, keyed by
            # employee_id only — never changes tracking/identity logic
            # above, just what's attached to the already-decided ID for
            # display. None name -> frontend shows "Person"/neutral color,
            # never a guessed company.
            name, color = employee_directory.get_display(display_identity)

            # Advisory only, and resolved independently of identity — a
            # track with no expression keeps its name, and a track with an
            # expression but no confident identity still shows nothing.
            expr, expr_conf = expression.service.get(self.camera_id, track_id)

            live_this_frame.append({
                "track_id": track_id,
                "bbox": list(pstate.bbox),
                "employee_id": display_identity,  # None -> frontend shows "Person", never "Unknown"
                "name": name,
                "color": color,
                "face_bbox": list(pstate.face_bbox) if pstate.face_bbox and (now - pstate.face_time) <= FACE_BOX_HOLD_SECONDS else None,
                "confidence": confidence,
                "identity_source": identity_source,  # face | appearance | None
                "expression": expr,
                "expression_confidence": expr_conf,
                # display-time inputs for get_live_detections (stripped there)
                "_disp": pstate.disp_box or tuple(float(v) for v in pstate.bbox),
                "_vel": pstate.vel,
                "_last_seen": pstate.last_seen_time,
            })

        # Replaced wholesale each frame (not merged) — a person who's
        # genuinely aged out (PERSON_TRACK_MAX_AGE) drops off the overlay on
        # the very next update; everyone still within that window keeps
        # showing at their last known position regardless of this frame's
        # raw detection result (see comment above).
        self._live_detections = live_this_frame
        done = time.time()
        if self.last_overlay_at:                       # smoothed time between detector cycles
            gap = done - self.last_overlay_at
            self._cycle_s = gap if not self.__dict__.get("_cycle_s") else 0.7 * self._cycle_s + 0.3 * gap
        self.last_overlay_at = done

        for tid in stale_persons:
            gone = self.person_tracks.pop(tid)
            self._diag_event("track_lost", track=tid, lived_s=round(now - (gone.first_seen_time or now), 1),
                             named=gone.current_identity is not None)
            # Don't let expression results accumulate for tracks that are gone.
            expression.service.forget(self.camera_id, tid)

    def _update_person_identity(self, pstate: PersonTrackState, frame: np.ndarray, face_row: np.ndarray,
                                body_frame: np.ndarray | None = None) -> None:
        """Throttled (LIVE_CLASSIFY_INTERVAL_FRAMES) classifier read on the
        face found inside this person's box, with a stability vote before
        committing/switching pstate.current_identity — see PersonTrackState
        docstring. No-op entirely when there is nothing to match against —
        neither a trained classifier nor any enrolled face — so the
        embedding cost is only paid once at least one of the two exists."""
        self._refresh_gallery()
        if self._get_classifier() is None and not self._gallery_cache:
            return
        # The throttle only applies once a person is already named — an
        # unnamed person is classified on every opportunity, so nobody waits
        # an extra cycle just because someone else was classified first.
        if pstate.current_identity is not None and self.frame_idx - pstate.last_classify_frame < LIVE_CLASSIFY_INTERVAL_FRAMES:
            return
        pstate.last_classify_frame = self.frame_idx

        fx1, fy1, fx2, fy2, _fconf = face_row
        fx1, fy1, fx2, fy2 = map(lambda v: max(0, int(v)), (fx1, fy1, fx2, fy2))
        px1, py1, px2, py2 = self._pad_bbox(fx1, fy1, fx2, fy2, frame.shape)
        crop = frame[py1:py2, px1:px2]
        if crop.size == 0:
            return

        # Reuse this exact crop for expression before spending it on the
        # embedding — same frame, same face, same track_id, so no second
        # detection pass anywhere. submit() is fire-and-forget and drops
        # the crop if its worker is busy, so this line cannot slow down
        # recognition below it (see expression.py).
        if analytics_settings.enabled("expression") and self.frame_idx - pstate.last_expression_frame >= EXPRESSION_INTERVAL_FRAMES:
            pstate.last_expression_frame = self.frame_idx
            expression.service.submit(self.camera_id, pstate.track_id, crop)

        job = (pstate, frame, crop, face_row, body_frame)
        q = self.__dict__.get("_recog_queue")
        if q is None:                       # no worker (tests): same work, inline
            self._recognize_face(*job)
            return
        if pstate.recog_inflight:           # this person's last job has not finished
            return
        pstate.recog_inflight = True
        try:
            q.put_nowait(job)
        except queue.Full:                  # bounded: newest work is skipped, never piled up
            pstate.recog_inflight = False

    def _recognize_face(self, pstate: PersonTrackState, frame: np.ndarray, crop: np.ndarray, face_row: np.ndarray,
                        body_frame: np.ndarray | None = None) -> None:
        """Embedding + identity match + stability vote for one face. Runs on the
        recognition worker (or inline without one); only ever touches the
        person's identity fields, never their box or existence."""
        fx1, fy1, fx2, fy2, _fconf = face_row
        fx1, fy1, fx2, fy2 = map(lambda v: max(0, int(v)), (fx1, fy1, fx2, fy2))
        px1, py1, px2, py2 = self._pad_bbox(fx1, fy1, fx2, fy2, frame.shape)
        embedding = self._embed(crop)
        if embedding is None and FACE_EMBED_RETRY and time.time() - pstate.last_embed_retry >= FACE_EMBED_RETRY_SECONDS:
            pstate.last_embed_retry = time.time()
            embedding = self._embed_with_border(crop)
        if embedding is None:
            fw, fh = fx2 - fx1, fy2 - fy1
            self._diag("FACE_TOO_SMALL" if fw * fh < MIN_FACE_AREA else "NO_EMBEDDING",
                       face_w=fw, face_h=fh, det_conf=float(_fconf), crop_w=px2 - px1, crop_h=py2 - py1)
            return

        predicted, score = self._identify_for_overlay(embedding)
        if predicted is None:
            m = self._last_match
            if m is not None:
                self._diag(m.reason or "NO_MATCH", best=m.best_person_id, score=float(m.score),
                           second=m.second_person_id, second_score=float(m.second_score),
                           margin=float(m.margin), refs=m.n_refs, face_w=fx2 - fx1, face_h=fy2 - fy1)
            # Not confident this cycle, from either source. Does NOT clear
            # an already-committed identity (the grace period, checked at
            # display time, handles that decay) — just resets the stability
            # vote so a run of weak reads can't slowly accumulate into a
            # wrong switch later. No "Unknown"/"Person" is ever produced
            # here: a failed recognition stays absent, and the overlay
            # renders nothing for it.
            pstate.pending_identity = None
            pstate.pending_count = 0
            return

        # Live-view reads mark attendance too, so someone is checked in the
        # moment they're recognised rather than when their face track ends.
        # (attendance.py applies its own, stricter confidence bar.)
        attendance.record(predicted, self.camera_id, score)

        if predicted == pstate.current_identity:
            # Already showing this person — just refresh the grace timer
            # and confidence, no need to re-run the stability vote.
            pstate.last_recognized_time = time.time()
            pstate.identity_confidence = score
            self._learn_appearance(predicted, body_frame if body_frame is not None else frame, pstate)
            return

        if predicted == pstate.pending_identity:
            pstate.pending_count += 1
        else:
            pstate.pending_identity = predicted
            pstate.pending_count = 1

        m = self._last_match
        strong_gallery = (self.__dict__.get("_last_source") == "gallery" and m is not None and m.person_id == predicted
                          and score >= INSTANT_COMMIT_SIMILARITY and m.margin >= INSTANT_COMMIT_MARGIN)
        if pstate.pending_count >= RECOGNITION_STABILITY_FRAMES or score >= INSTANT_COMMIT_PROBA or strong_gallery:
            pstate.current_identity = predicted
            pstate.identity_confidence = score
            pstate.last_recognized_time = time.time()
            self._learn_appearance(predicted, body_frame if body_frame is not None else frame, pstate)

    def _learn_appearance(self, employee_id: str, frame: np.ndarray, pstate: PersonTrackState) -> None:
        """A face just confirmed who this is: remember their body appearance
        for today so faceless sightings elsewhere can be named (appearance.py)."""
        if not analytics_settings.enabled("appearance_handoff"):
            return
        try:
            appearance.gallery.learn(employee_id, frame, pstate.bbox)
        except Exception:
            log.exception("camera %s: learning appearance failed", self.camera_id)

    def _update_appearance_identities(self, frame: np.ndarray, track_ids: list[int]) -> None:
        """Name people with no current face identity by body appearance.
        Face identity always takes precedence (see the display code)."""
        if not analytics_settings.enabled("appearance_handoff") or not appearance.gallery.known_count():
            return
        now = time.time()
        # Employees already shown by face on this camera can't also be someone else here.
        taken = {p.current_identity for p in self.person_tracks.values()
                 if p.current_identity and now - p.last_recognized_time <= IDENTITY_GRACE_SECONDS}
        tries = 0
        for tid in track_ids:
            if tries >= APPEARANCE_MATCHES_PER_CYCLE:
                break
            p = self.person_tracks[tid]
            has_face_identity = p.current_identity and now - p.last_recognized_time <= IDENTITY_GRACE_SECONDS
            if has_face_identity or now - p.appearance_last_try < APPEARANCE_INTERVAL_SECONDS:
                continue
            p.appearance_last_try = now
            tries += 1
            try:
                emp, score = appearance.gallery.match(frame, p.bbox, exclude=taken)
            except Exception:
                log.exception("camera %s: appearance match failed", self.camera_id)
                return
            if emp is None:
                p.appearance_pending, p.appearance_pending_count = None, 0
                continue
            if emp == p.appearance_identity:
                p.appearance_time, p.appearance_confidence = now, score
                continue
            if emp == p.appearance_pending:
                p.appearance_pending_count += 1
            else:
                p.appearance_pending, p.appearance_pending_count = emp, 1
            if p.appearance_pending_count >= APPEARANCE_STABILITY:
                p.appearance_identity, p.appearance_confidence, p.appearance_time = emp, score, now
                taken.add(emp)

    def _identify(self, frame: np.ndarray, bbox) -> tuple[str | None, float]:
        """Employee id + classifier probability for one face box, for
        desks.py. (None, 0.0) if there's no trained classifier or no clean
        aligned face in the crop."""
        clf = self._get_classifier()
        if clf is None:
            return None, 0.0
        x1, y1, x2, y2 = (max(0, int(v)) for v in bbox)
        px1, py1, px2, py2 = self._pad_bbox(x1, y1, x2, y2, frame.shape)
        crop = frame[py1:py2, px1:px2]
        if crop.size == 0:
            return None, 0.0
        embedding = self._embed(crop)
        if embedding is None:
            return None, 0.0
        proba = clf.predict_proba(embedding.reshape(1, -1))[0]
        best = int(np.argmax(proba))
        return str(clf.classes_[best]), float(proba[best])

    def _identify_face(self, frame: np.ndarray, bbox) -> tuple[str | None, float]:
        """Employee id + confidence for one face box, with the live-overlay
        rules (_identify_for_overlay: classifier, then the enrolment gallery,
        each with its own threshold). (None, 0.0) when not confident."""
        x1, y1, x2, y2 = (max(0, int(v)) for v in bbox)
        px1, py1, px2, py2 = self._pad_bbox(x1, y1, x2, y2, frame.shape)
        crop = frame[py1:py2, px1:px2]
        if crop.size == 0:
            return None, 0.0
        self._ensure_models_loaded()
        embedding = self._embed(crop)
        if embedding is None:
            return None, 0.0
        return self._identify_for_overlay(embedding)

    def _identify_head(self, frame: np.ndarray, head_box) -> tuple[str | None, float, str]:
        """Identity for the head part of a body box (footfall): finds the face
        in it first, then identifies that tight face box exactly like the
        visitor and overlay paths (_identify_face). Handing the whole head area
        to _identify_face instead pads it further and leaves the face too small
        to match. Returns (employee | None, confidence, outcome) where outcome
        is "no face" | "not confident" | "match"."""
        self._ensure_models_loaded()
        det = getattr(self._arcface, "det_model", None)
        x1, y1, x2, y2 = (max(0, int(v)) for v in head_box)
        crop = frame[y1:y2, x1:x2]
        if det is None or crop.size == 0:
            return None, 0.0, "no face"
        faces, _ = det.detect(crop, input_size=(256, 256))
        if faces is None or len(faces) == 0:
            return None, 0.0, "no face"
        fx1, fy1, fx2, fy2 = max(faces, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))[:4]
        employee, confidence = self._identify_face(frame, [x1 + fx1, y1 + fy1, x1 + fx2, y1 + fy2])
        return employee, confidence, "match" if employee else "not confident"

    def _identify_for_overlay(self, embedding: np.ndarray) -> tuple[str | None, float]:
        """Identity for ONE face embedding on the live overlay path, using
        both recognition sources in priority order:

        1. The trained classifier — the primary path, unchanged. Accepted
           only at/above CLASSIFIER_MIN_PROBA.
        2. Fallback: nearest-neighbour cosine match against the local
           Identity enrollment gallery (face_db's face_embeddings — the
           same storage POST /api/faces/enroll writes to, reused here
           rather than introducing a second gallery). Accepted only
           at/above MATCH_THRESHOLD. This is what lets someone who IS
           enrolled on the Identity page but has too few labelled camera
           captures to be one of the classifier's classes still be
           recognised live.

        Returns (None, 0.0) when neither source is confident enough. That
        is a real "no identity" answer and is deliberately never converted
        into an "Unknown"/"Person" label — the caller leaves the track
        unidentified and the overlay renders nothing for it.

        Distinct from _match() (used by the capture/review path), which
        treats the gallery as the mode to use *instead of* a classifier
        when none has been trained; here the gallery is an additional
        source consulted whenever the classifier isn't sure.
        """
        self._last_match = None
        self._refresh_gallery()
        res = face_matching.match(self._gallery_index, embedding, LIVE_MATCH_THRESHOLD)
        self._last_match = res
        clf = self._get_classifier()
        if clf is not None:
            proba = clf.predict_proba(embedding.reshape(1, -1))[0]
            best_idx = int(np.argmax(proba))
            score = float(proba[best_idx])
            if score >= CLASSIFIER_MIN_PROBA:
                predicted = str(clf.classes_[best_idx])
                # The classifier is closed-set: it only knows the people it was
                # last trained on and always answers with one of them. Measured
                # on this install, several people enrolled AFTER the last
                # training run were named as someone else at >= 0.6 (one on 7 of
                # their 8 enrollment photos). A confident gallery match to a
                # DIFFERENT person the classifier does NOT know is evidence it has
                # never seen this face, so the gallery wins. For a person the
                # classifier WAS trained on, the classifier (trained on real CCTV
                # captures) is the better judge: letting the weaker photo match
                # override it named Pushpendra as Alok/Ravi. Measured on 1,265
                # labelled captures: 30 wrong names with any override, 6 with this.
                if (res.person_id is not None and res.person_id != predicted
                        and res.person_id not in {str(c) for c in clf.classes_}):
                    self._last_source = "gallery"
                    return res.person_id, res.score
                self._last_source = "classifier"
                return predicted, score

        if res.person_id is not None:
            self._last_source = "gallery"
            return res.person_id, res.score
        self._last_source = None
        return None, 0.0

    def _tick(self, stage: str, started: float) -> None:
        """Smoothed wall-clock cost (ms) of one pipeline stage, for perf_snapshot()."""
        ms = (time.perf_counter() - started) * 1000.0
        d = self.__dict__.setdefault("stage_ms", {})
        prev = d.get(stage)
        d[stage] = ms if prev is None else prev * 0.8 + ms * 0.2

    def perf_snapshot(self) -> dict[str, float]:
        """{stage: smoothed ms} - face_detect, person_detect, rescue, recognise."""
        return {k: round(v, 1) for k, v in self.__dict__.get("stage_ms", {}).items()}

    def close(self) -> None:
        """Stop this pipeline's background workers (called when it is discarded)."""
        self._face_closed = True
        ev = self.__dict__.get("_face_event")
        if ev is not None:
            ev.set()
        q = self.__dict__.get("_recog_queue")
        if q is not None:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        pool = self.__dict__.get("_rescue_pool")
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _recog_worker(self) -> None:
        while True:
            job = self._recog_queue.get()
            if job is None:
                return
            pstate = job[0]
            started = time.perf_counter()
            try:
                self._recognize_face(*job)
            except Exception:
                log.exception("camera %s: face recognition worker failed", self.camera_id)
            finally:
                pstate.recog_inflight = False
                self._tick("recognise", started)

    def _diag(self, reason: str, **fields) -> None:
        """Count one failed recognition attempt and, with FACE_DIAG on, log it
        (at most once per FACE_DIAG_INTERVAL_SECONDS per reason on this
        camera). Fields are numbers and employee ids only - no images, no
        stream URLs, no names."""
        self._diag_counts[reason] = self._diag_counts.get(reason, 0) + 1
        if not FACE_DIAG:
            return
        now = time.time()
        if now - self._diag_last_log.get(reason, 0.0) < FACE_DIAG_INTERVAL_SECONDS:
            return
        self._diag_last_log[reason] = now
        log.info("face_diag camera=%s reason=%s seen=%d %s", self.camera_id, reason,
                 self._diag_counts[reason],
                 " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in fields.items()))

    def diag_snapshot(self) -> dict[str, int]:
        """{rejection reason: how many attempts since this pipeline started}."""
        return dict(self._diag_counts)

    def get_live_detections(self) -> list[dict]:
        """Snapshot for the /ws/detections/{camera_id} route — see the
        _live_detections attribute comment for the threading contract.
        Person-track-based (see _update_person_overlay), not face-track.

        Evaluated at the moment it is asked for, not only when the pipeline
        last finished a cycle: a person's box is moved along their recent
        velocity for at most PERSON_PREDICT_MAX_SECONDS (damped), and a box
        whose person was last actually detected more than
        PERSON_TRACK_MAX_SECONDS ago is dropped - so boxes stay put and glide
        while the pipeline is busy, and still disappear on their own once
        someone has really left, even if the pipeline has stalled."""
        now = time.time()
        hold = self._track_hold()
        fw, fh = self.frame_size if self.frame_size else (None, None)
        out = []
        for d in self._live_detections:
            age = now - d["_last_seen"]
            if age > hold:
                continue
            x1, y1, x2, y2 = d["_disp"]
            vx, vy = d["_vel"]
            lead = max(0.0, min(age, PERSON_PREDICT_MAX_SECONDS)) * 0.5   # damped
            dx, dy = vx * lead, vy * lead
            box = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
            if fw:
                box = [min(max(box[0], 0), fw), min(max(box[1], 0), fh), min(max(box[2], 0), fw), min(max(box[3], 0), fh)]
            item = {k: v for k, v in d.items() if not k.startswith("_")}
            item["bbox"] = [int(round(v)) for v in box]
            out.append(item)
        return out

    @staticmethod
    def _smooth_box(pstate: "PersonTrackState", new, now: float) -> None:
        """Exponential smoothing of the displayed box towards the new detection,
        plus a velocity estimate of its centre. A big jump (a different place,
        not a step) resets instead of smearing."""
        new = tuple(float(v) for v in new)
        old = pstate.disp_box
        if old is None or CameraFacePipeline._iou(np.array(old), np.array(new)) < 0.15:
            pstate.disp_box, pstate.vel = new, (0.0, 0.0)
            return
        a = min(max(PERSON_BOX_SMOOTHING, 0.05), 1.0)
        sm = tuple(o + a * (n - o) for o, n in zip(old, new))
        dt = max(now - (pstate.last_seen_time or now), 0.05)
        vx = ((sm[0] + sm[2]) - (old[0] + old[2])) / 2.0 / dt
        vy = ((sm[1] + sm[3]) - (old[1] + old[3])) / 2.0 / dt
        # blend with the previous estimate and cap, so one noisy step cannot
        # fling the predicted box across the picture
        cap = 0.5 * max(new[2] - new[0], 1.0) / max(PERSON_PREDICT_MAX_SECONDS, 0.1)
        pstate.vel = (max(-cap, min(cap, 0.5 * pstate.vel[0] + 0.5 * vx)),
                      max(-cap, min(cap, 0.5 * pstate.vel[1] + 0.5 * vy)))
        pstate.disp_box = sm

    def _diag_event(self, event: str, **fields) -> None:
        """Track lifecycle line (FACE_DIAG only, no rate limit - these are rare)."""
        if FACE_DIAG:
            log.info("face_diag camera=%s event=%s %s", self.camera_id, event,
                     " ".join(f"{k}={v}" for k, v in fields.items()))

    def _embed(self, crop: np.ndarray) -> np.ndarray | None:
        """Run InsightFace's own detector on the crop to get landmarks and
        alignment right, then extract the ArcFace embedding. This
        re-detection-on-the-crop step is deliberate: YOLO gave us a fast
        rough box for tracking, but ArcFace's accuracy depends heavily on
        proper 5-point alignment, which InsightFace's FaceAnalysis handles
        internally. Skipping this (feeding YOLO's raw crop straight into an
        embedding model) is the most common cause of "everyone is unknown."
        """
        det = getattr(self._arcface, "det_model", None)
        rec = getattr(self._arcface, "models", {}).get("recognition")
        if det is None or rec is None or FACE_EMBED_DET_SIZE <= 0:
            faces = self._arcface.get(crop)
            if not faces:
                return None
            # Largest face in the crop (should be the only one)
            face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            return face.normed_embedding  # already L2-normalized, 512-dim
        # Same steps as FaceAnalysis.get() (detect -> 5-point landmarks -> aligned
        # ArcFace embedding), with the detector run at FACE_EMBED_DET_SIZE.
        from insightface.app.common import Face

        size = (FACE_EMBED_DET_SIZE, FACE_EMBED_DET_SIZE)
        bboxes, kpss = det.detect(crop, input_size=size, max_num=0, metric="default")
        if bboxes.shape[0] == 0 or kpss is None:
            return None
        i = int(np.argmax((bboxes[:, 2] - bboxes[:, 0]) * (bboxes[:, 3] - bboxes[:, 1])))
        face = Face(bbox=bboxes[i, 0:4], kps=kpss[i], det_score=bboxes[i, 4])
        rec.get(crop, face)
        return face.normed_embedding

    def _embed_with_border(self, crop: np.ndarray) -> np.ndarray | None:
        """_embed() on the crop with grey background added all round - the retry
        for faces InsightFace's detector misses when they fill their crop."""
        h, w = crop.shape[:2]
        by, bx = int(h * FACE_EMBED_RETRY_BORDER), int(w * FACE_EMBED_RETRY_BORDER)
        return self._embed(cv2.copyMakeBorder(crop, by, by, bx, bx, cv2.BORDER_CONSTANT, value=(114, 114, 114)))

    def _match(self, embedding: np.ndarray) -> tuple[str | None, float]:
        """Two modes, tried in this order:
        1. Trained classifier (POST /api/faces/training/train has been run
           at least once) — a supervised decision boundary learned from
           real, human-labeled camera captures. score is predict_proba for
           the winning class, compared against FACE_CLASSIFIER_MIN_PROBA.
        2. Fallback: nearest-neighbor cosine similarity against whatever's
           in face_embeddings (enrollment photos + assigned review
           captures), compared against MATCH_THRESHOLD. This is the only
           mode available before any training run.
        Both return (person_id | None, score) so callers don't need to know
        which mode produced it — MATCH_THRESHOLD vs. CLASSIFIER_MIN_PROBA is
        handled here, not by the caller.
        """
        clf = self._get_classifier()
        if clf is not None:
            proba = clf.predict_proba(embedding.reshape(1, -1))[0]
            best_idx = int(np.argmax(proba))
            return str(clf.classes_[best_idx]), float(proba[best_idx])

        self._refresh_gallery()
        if not self._gallery_cache:
            return None, 0.0
        best_person, best_score = None, -1.0
        for entry in self._gallery_cache:
            gallery_vec = np.array(entry["embedding"], dtype=np.float32)
            score = float(np.dot(embedding, gallery_vec))  # cosine sim (both normalized)
            if score > best_score:
                best_score = score
                best_person = entry["person_id"]
        return best_person, best_score

    def _suggest_identity(self, embedding: np.ndarray) -> dict:
        """Who this face most likely is, with how sure we are -- recorded with
        the capture so the dataset can be grouped by ID. Returns a dict with
        employee_id (None = unknown), confidence, margin, tier:
          auto     reliable: above AUTO_ASSIGN_* and clear of the runner-up
          review   plausible (>= the live match bar) but not reliable enough
          unknown  nobody enrolled is close enough
        Never raises, and never labels anything: it is a suggestion only."""
        try:
            clf = self._get_classifier()
            if clf is not None:
                proba = clf.predict_proba(embedding.reshape(1, -1))[0]
                order = np.argsort(proba)[::-1]
                best = float(proba[order[0]])
                second = float(proba[order[1]]) if len(order) > 1 else 0.0
                person = str(clf.classes_[order[0]])
                if best >= AUTO_ASSIGN_CLASSIFIER_PROBA and best - second >= AUTO_ASSIGN_MARGIN:
                    tier = "auto"
                elif best >= CLASSIFIER_MIN_PROBA:
                    tier = "review"
                else:
                    return {"employee_id": None, "confidence": best, "margin": best - second, "tier": "unknown"}
                return {"employee_id": person, "confidence": best, "margin": best - second, "tier": tier}

            self._refresh_gallery()
            return face_dataset.suggest_from_gallery(
                embedding, self._gallery_cache, AUTO_ASSIGN_THRESHOLD, MATCH_THRESHOLD, AUTO_ASSIGN_MARGIN
            )
        except Exception:
            log.exception("camera %s: identity suggestion failed", self.camera_id)
            return {"employee_id": None, "confidence": 0.0, "margin": 0.0, "tier": "unknown"}

    def _match_threshold(self) -> float:
        return CLASSIFIER_MIN_PROBA if self._get_classifier() is not None else MATCH_THRESHOLD

    def _is_recent_duplicate(self, embedding: np.ndarray) -> bool:
        """True if `embedding` is highly similar to one captured on THIS
        camera within the last DEDUP_COOLDOWN_SECONDS — almost certainly the
        same physical appearance re-surfacing under a new track_id (tracking
        flicker, brief occlusion, a quick RTSP reconnect), not a genuinely
        new sighting. Deliberately narrow in scope:
          - per-camera only (each pipeline instance has its own list)
          - cooldown window (default 900s / 15 min for multi-day runs — see
            DEDUP_COOLDOWN_SECONDS) — does NOT suppress the same person
            appearing again later in the day, nor on a different camera
          - a single cosine-similarity check against recent embeddings,
            reusing the exact same math _match() already uses — no new
            model, no employee_id involved, nothing is auto-labeled.
        """
        now = time.time()
        self._recent_captures = [
            (t, e) for t, e in self._recent_captures if now - t <= DEDUP_COOLDOWN_SECONDS
        ]
        for _, recent_embedding in self._recent_captures:
            if float(np.dot(embedding, recent_embedding)) >= DEDUP_SIMILARITY_THRESHOLD:
                return True
        return False

    def _save_training_capture(self, state: TrackState, embedding: np.ndarray | None, suggestion: dict | None = None):
        """Every finished track lands here, unconditionally — confident
        match, low-confidence, or no embedding at all. This is the bulk
        dataset for manual labeling; face_pending (below) stays a separate,
        narrower live-correction queue. Never lets a failure here stop
        recognition — worst case, this capture is silently missing from the
        training set.

        Gates, in order:
          1. Cross-track duplicate check (embedding-based, see
             _is_recent_duplicate) — skips entirely, no file, no DB row.
             Counted (face_db.increment_running_session_duplicates_rejected)
             purely for monitoring visibility into how much this is
             happening; the count is session-scoped, not enforcement.
          2. MAX_TRAINING_CAPTURES — a global, all-cameras, all-statuses
             ceiling — and MAX_CAPTURES_PER_CAMERA — this camera's share of
             it — both checked+enforced atomically via
             face_db.capture_limit_lock so concurrent camera threads can't
             overshoot either.
          3. label_status is decided from embedding presence + quality
             metrics: only a capture with a real embedding AND acceptable
             blur/brightness/face-size becomes 'unlabeled' (the only status
             /face-training's queue ever shows). Everything else is still
             saved (nothing is deleted) as 'no_embedding' or 'rejected' for
             diagnostics, but is invisible to the labeling queue and
             excluded from classifier training.
        """
        try:
            if embedding is not None and self._is_recent_duplicate(embedding):
                try:
                    face_db.increment_running_session_duplicates_rejected()
                except Exception:
                    log.debug("could not bump duplicate counter", exc_info=True)
                return

            with face_db.capture_limit_lock:
                if face_db.count_training_captures() >= MAX_TRAINING_CAPTURES:
                    _warn_capture_cap_reached()
                    return
                if face_db.count_training_captures_for_camera(self.camera_id) >= MAX_CAPTURES_PER_CAMERA:
                    return

                blur, brightness = self._quality_metrics(state.best_crop)

                if embedding is None:
                    label_status = "no_embedding"
                elif (
                    blur < MIN_BLUR_SCORE
                    or not (MIN_BRIGHTNESS <= brightness <= MAX_BRIGHTNESS)
                    or state.best_area < MIN_FACE_AREA
                ):
                    label_status = "rejected"
                else:
                    label_status = "unlabeled"

                capture_dir = os.path.join(TRAINING_UNLABELED_DIR, time.strftime("%Y-%m-%d"), f"cam{self.camera_id}")
                os.makedirs(capture_dir, exist_ok=True)
                image_path = os.path.join(capture_dir, f"cam{self.camera_id}_track{state.track_id}_{uuid.uuid4().hex[:8]}.jpg")
                write_ok = cv2.imwrite(image_path, state.best_crop)
                # Never create a DB row for an image that isn't actually
                # sitting on disk — cv2.imwrite() can return True yet still
                # not leave a readable file behind on some setups (e.g. a
                # synced/cloud-backed folder interfering with a burst of
                # small file writes), which previously produced orphaned
                # rows whose /face-training image would silently 404. Both
                # checks: the call's own return value, and a real stat.
                if not write_ok or not os.path.exists(image_path):
                    log.error(
                        "camera %s: wrote training capture image but it's not on disk afterward (%s) — dropping this capture",
                        self.camera_id, image_path,
                    )
                    return
                face_db.add_training_capture(
                    camera_id=self.camera_id,
                    track_id=state.track_id,
                    image_path=storage.to_stored(image_path),
                    embedding=embedding.tolist() if embedding is not None else None,
                    detection_confidence=state.best_conf,
                    blur_score=blur,
                    brightness=brightness,
                    label_status=label_status,
                    auto_employee_id=suggestion["employee_id"] if suggestion else None,
                    auto_confidence=suggestion["confidence"] if suggestion else None,
                    auto_margin=suggestion["margin"] if suggestion else None,
                    auto_tier=suggestion["tier"] if suggestion else None,
                    predicted_name=(
                        (employee_directory.get_employee(suggestion["employee_id"]) or {}).get("name")
                        if suggestion and suggestion["employee_id"] else None
                    ),
                )

            try:
                face_db.touch_running_collection_session()
            except Exception:
                log.debug("could not touch collection session", exc_info=True)

            if embedding is not None:
                self._recent_captures.append((time.time(), embedding))
        except Exception:
            # Never let a failed capture save disturb recognition, but never
            # hide it either (disk full, permissions, DB locked...).
            log.exception("camera %s: saving a training capture failed", self.camera_id)

    def _recognize_and_route(self, state: TrackState):
        embedding = self._embed(state.best_crop)

        # Bulk dataset collection — unconditional, regardless of whether a
        # clean embedding or a confident match came out of this track.
        suggestion = self._suggest_identity(embedding) if embedding is not None else None
        self._save_training_capture(state, embedding, suggestion)

        if embedding is None:
            return  # couldn't get a clean aligned face out of the crop; drop it

        person_id, score = self._match(embedding)

        if person_id is not None and score >= self._match_threshold():
            # Confident match: no human needed. Marks attendance (attendance.py
            # applies its own, stricter confidence bar).
            attendance.record(person_id, self.camera_id, score)
            return

        # A face nobody recognises at a gate is an "Unknown Person" alert
        # (alerts.py decides: gate cameras only, clearly-unknown only, rate-limited).
        ok, jpg = cv2.imencode(".jpg", state.best_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        alerts.unknown_face(self.camera_id, score if person_id is not None else 0.0, jpg.tobytes() if ok else None)

        # Below threshold (or empty gallery) -> queue for human review,
        # but don't spam the same track twice within REVIEW_DEDUPE_SECONDS.
        since = time.time() - REVIEW_DEDUPE_SECONDS
        if face_db.has_pending_for_track(self.camera_id, state.track_id, since):
            return

        image_path = os.path.join(
            CAPTURE_DIR, f"cam{self.camera_id}_track{state.track_id}_{uuid.uuid4().hex[:8]}.jpg"
        )
        cv2.imwrite(image_path, state.best_crop)

        face_db.add_pending(
            camera_id=self.camera_id,
            track_id=state.track_id,
            image_path=storage.to_stored(image_path),
            embedding=embedding.tolist(),
            best_match_person_id=person_id,
            best_match_score=score if person_id else None,
        )
        state.pushed_to_review = True


# ---------------------------------------------------------------------------
# Registry: one pipeline instance per camera, reused across frames
# ---------------------------------------------------------------------------

_pipelines: dict[int, CameraFacePipeline] = {}
_pipelines_lock = threading.Lock()


# Building a pipeline loads models and can take many seconds on a busy machine.
# It must never happen while _pipelines_lock is held: /ws/detections looks pipelines
# up from the server's event loop, and a lookup that waited on a lock held through
# a model load froze every API request and websocket for that long (seen live: the
# whole portal stopped answering and showed no cameras). So the registry lock only
# ever guards dict operations; construction is serialised per camera instead.
_creating: dict[int, threading.Lock] = {}


def get_pipeline(camera_id: int) -> CameraFacePipeline:
    p = _pipelines.get(camera_id)
    if p is not None:
        return p
    with _pipelines_lock:
        creating = _creating.setdefault(camera_id, threading.Lock())
    with creating:                                   # one construction per camera at a time
        p = _pipelines.get(camera_id)
        if p is None:
            p = CameraFacePipeline(camera_id)        # slow: models, workers
            with _pipelines_lock:
                _pipelines[camera_id] = p
    return p


_cap_warned_at = 0.0


def _warn_capture_cap_reached() -> None:
    """Collection stops at MAX_TRAINING_CAPTURES; say so (hourly), and see
    retention.py / FACE_CAPTURE_RETENTION_DAYS for how space is reclaimed."""
    global _cap_warned_at
    now = time.time()
    if now - _cap_warned_at > 3600:
        _cap_warned_at = now
        log.warning("training capture limit reached (%d); no new training captures are being saved. "
                    "Label or skip the queue, or let retention remove old unlabeled/rejected captures.",
                    MAX_TRAINING_CAPTURES)


def discard_pipeline(camera_id: int) -> None:
    """Drop one camera's pipeline (tracker/temporal state) so the next frame
    builds a fresh one — used by camera_stream's recovery after repeated
    failures. The shared model instances are class-level and untouched, so
    other cameras are unaffected; any model that failed to load is None and
    gets loaded again by the new pipeline."""
    with _pipelines_lock:
        old = _pipelines.pop(camera_id, None)
    if old is not None:
        old.close()


def get_existing_pipeline(camera_id: int) -> "CameraFacePipeline | None":
    """Non-creating lookup for /ws/detections — that route must never spin
    up a full pipeline (loads YOLO+ArcFace) on its own just because a
    browser tab opened the overlay socket; a pipeline only ever gets created
    by camera_stream.py once /ws/live's RTSP read loop for that camera is
    actually running.

    Lock-free on purpose: it is called from the event loop and must never wait
    for a pipeline that is still being built (a dict read is atomic)."""
    return _pipelines.get(camera_id)
