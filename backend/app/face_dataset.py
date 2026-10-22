"""
face_dataset.py

Pure helpers (no model is loaded; embeddings are stored with each capture) for
two things:

  1. suggest_from_gallery   the machine's SUGGESTION of who a face is, among the
                            enrolled people. Only ever a suggestion: it is stored
                            next to a capture, never as its label.
  2. The curated, VERIFIED-ONLY training material:
       build_verified_dataset   copies human-approved captures into
                                <dataset>/<employee_id>/ (+ manifest.csv)
       plan_gallery_update      which verified captures would add new information
                                to a person's enrollment gallery, and whether
                                adding them helps, hurts or is untestable
       (the DB write + backup live in apply_gallery_update / restore_gallery)

Rule enforced here and by the callers: a capture is training material only if a
person approved or corrected it (face_db.list_verified_captures). A prediction,
an unknown, a pending or a deleted capture never is.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from dataclasses import dataclass, field

import numpy as np

# Two captures this similar are the same moment, not new information.
NEAR_DUPLICATE_SIMILARITY = 0.92
# Adding a capture to the gallery only helps if it shows something the gallery
# doesn't already hold.
GALLERY_ADD_MAX_SIMILARITY = 0.85
GALLERY_MAX_PER_PERSON = 30
# An update is only accepted when it can be tested on at least this many
# verified captures that were NOT added to the gallery.
GALLERY_MIN_HELD_OUT = 10


def suggest_from_gallery(
    embedding: np.ndarray,
    gallery: list[dict],
    assign_threshold: float,
    review_threshold: float,
    margin_required: float,
) -> dict:
    """gallery: [{"person_id": str, "embedding": list|ndarray (L2-normalised)}].
    Returns {employee_id|None, confidence, margin, tier}; tier is
    auto | review | unknown."""
    per_person: dict[str, float] = {}
    for entry in gallery:
        sim = float(np.dot(embedding, np.asarray(entry["embedding"], dtype=np.float32)))
        if sim > per_person.get(entry["person_id"], -1.0):
            per_person[entry["person_id"]] = sim
    if not per_person:
        return {"employee_id": None, "confidence": 0.0, "margin": 0.0, "tier": "unknown"}
    ranked = sorted(per_person.items(), key=lambda kv: kv[1], reverse=True)
    person, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = best - second if second > -1.0 else best
    if best >= assign_threshold and margin >= margin_required:
        tier = "auto"
    elif best >= review_threshold:
        tier = "review"
    else:
        return {"employee_id": None, "confidence": best, "margin": margin, "tier": "unknown"}
    return {"employee_id": person, "confidence": best, "margin": margin, "tier": tier}


def identify(embedding: np.ndarray, gallery: list[dict], threshold: float) -> str | None:
    """Nearest enrolled person, or None when nobody reaches `threshold`."""
    best, who = -1.0, None
    for entry in gallery:
        sim = float(np.dot(embedding, np.asarray(entry["embedding"], dtype=np.float32)))
        if sim > best:
            best, who = sim, entry["person_id"]
    return who if best >= threshold else None


# ---------------------------------------------------------------------------
# Curated dataset (verified captures only)
# ---------------------------------------------------------------------------

@dataclass
class Item:
    capture_id: int
    employee_id: str
    decision: str  # kept | excluded
    reason: str
    src: str | None = None


def _unusable_reason(row: dict, min_blur: float, min_brightness: float, max_brightness: float) -> str | None:
    if not row.get("embedding"):
        return "no embedding"
    if row.get("blur_score") is None or row["blur_score"] < min_blur:
        return "blurry"
    b = row.get("brightness")
    if b is None or not (min_brightness <= b <= max_brightness):
        return "bad brightness"
    return None


def build_verified_dataset(
    rows: list[dict],
    roster_ids: set[str],
    resolve_path,
    out_dir: str,
    *,
    min_blur: float,
    min_brightness: float,
    max_brightness: float,
    apply: bool,
) -> tuple[list[Item], dict]:
    """rows must already be human-verified (face_db.list_verified_captures).
    The verified id must exist in the active roster. Near-duplicates and
    low-quality images are excluded. With apply=True the folder
    <out_dir>/<employee_id>/ is made to match exactly the kept set (stale copies
    from since-deleted captures are removed; only files inside out_dir are
    ever touched) and manifest.csv is written."""
    items: list[Item] = []
    kept_vecs: dict[str, list[np.ndarray]] = {}
    for row in sorted(rows, key=lambda r: -(r.get("detection_confidence") or 0.0)):
        who = row["verified_employee_id"]
        src = resolve_path(row["image_path"])
        reason = _unusable_reason(row, min_blur, min_brightness, max_brightness)
        if reason is None and who not in roster_ids:
            reason = "verified id is not in the active roster"
        if reason is None and (src is None or not os.path.exists(src)):
            reason = "image file missing"
        if reason is None:
            emb = np.asarray(json.loads(row["embedding"]), dtype=np.float32)
            if any(float(np.dot(emb, k)) >= NEAR_DUPLICATE_SIMILARITY for k in kept_vecs.get(who, [])):
                reason = "near-duplicate of an already kept image"
            else:
                kept_vecs.setdefault(who, []).append(emb)
        items.append(Item(row["id"], who, "excluded" if reason else "kept", reason or "verified", str(src) if src else None))

    kept = [i for i in items if i.decision == "kept"]
    summary = {
        "verified_input": len(rows),
        "kept": len(kept),
        "excluded": len(items) - len(kept),
        "per_person": {w: sum(1 for i in kept if i.employee_id == w) for w in sorted({i.employee_id for i in kept})},
        "excluded_reasons": {},
    }
    for i in items:
        if i.decision == "excluded":
            summary["excluded_reasons"][i.reason] = summary["excluded_reasons"].get(i.reason, 0) + 1

    if apply:
        out_root = os.path.abspath(out_dir)
        os.makedirs(out_root, exist_ok=True)
        wanted = {(i.employee_id, os.path.basename(i.src)) for i in kept}
        for who in {i.employee_id for i in kept} | {d for d in os.listdir(out_root) if os.path.isdir(os.path.join(out_root, d))}:
            folder = os.path.abspath(os.path.join(out_root, who))
            if os.path.commonpath([out_root, folder]) != out_root or not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if (who, name) not in wanted:
                    os.remove(os.path.join(folder, name))
        for i in kept:
            folder = os.path.join(out_root, i.employee_id)
            os.makedirs(folder, exist_ok=True)
            shutil.copy2(i.src, os.path.join(folder, os.path.basename(i.src)))
        with open(os.path.join(out_root, "manifest.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["capture_id", "verified_employee_id", "decision", "reason"])
            for i in items:
                w.writerow([i.capture_id, i.employee_id, i.decision, i.reason])
    return items, summary


# ---------------------------------------------------------------------------
# Embedding-gallery update (explicit, backed up, validated)
# ---------------------------------------------------------------------------

@dataclass
class GalleryPlan:
    additions: list[tuple[str, int, list[float], str]] = field(default_factory=list)  # (employee_id, capture_id, embedding, image_path)
    held_out: int = 0
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    verdict: str = "no_candidates"  # ok | no_candidates | untestable | would_degrade
    detail: str = ""


def _score(eval_rows: list[tuple[str, np.ndarray]], gallery: list[dict], threshold: float) -> dict:
    correct = wrong = unknown = 0
    for who, emb in eval_rows:
        got = identify(emb, gallery, threshold)
        if got == who:
            correct += 1
        elif got is None:
            unknown += 1
        else:
            wrong += 1
    return {"correct": correct, "wrong_id": wrong, "unknown": unknown, "n": len(eval_rows)}


def plan_gallery_update(kept: list[Item], rows_by_id: dict[int, dict], gallery: list[dict], match_threshold: float) -> GalleryPlan:
    """Choose verified captures that add NEW information (not too similar to
    what the person's gallery already holds) and test the result on the verified
    captures that were NOT added. The update is only 'ok' when it can be tested
    and does not increase wrong IDs or lose correct ones."""
    plan = GalleryPlan()
    have: dict[str, list[np.ndarray]] = {}
    for g in gallery:
        have.setdefault(g["person_id"], []).append(np.asarray(g["embedding"], dtype=np.float32))
    chosen: set[int] = set()
    for it in kept:
        pool = have.setdefault(it.employee_id, [])
        if len(pool) >= GALLERY_MAX_PER_PERSON:
            continue
        emb = np.asarray(json.loads(rows_by_id[it.capture_id]["embedding"]), dtype=np.float32)
        if any(float(np.dot(emb, v)) > GALLERY_ADD_MAX_SIMILARITY for v in pool):
            continue
        pool.append(emb)
        chosen.add(it.capture_id)
        plan.additions.append((it.employee_id, it.capture_id, emb.tolist(), rows_by_id[it.capture_id]["image_path"]))
    if not plan.additions:
        plan.verdict, plan.detail = "no_candidates", "no verified capture adds anything the gallery does not already hold"
        return plan

    eval_rows = [
        (it.employee_id, np.asarray(json.loads(rows_by_id[it.capture_id]["embedding"]), dtype=np.float32))
        for it in kept if it.capture_id not in chosen
    ]
    plan.held_out = len(eval_rows)
    if len(eval_rows) < GALLERY_MIN_HELD_OUT:
        plan.verdict = "untestable"
        plan.detail = (f"only {len(eval_rows)} verified captures are left over to test the update on "
                       f"(need {GALLERY_MIN_HELD_OUT}); approve more captures first")
        return plan
    after_gallery = gallery + [{"person_id": w, "embedding": e} for w, _c, e, _p in plan.additions]
    plan.before = _score(eval_rows, gallery, match_threshold)
    plan.after = _score(eval_rows, after_gallery, match_threshold)
    if plan.after["wrong_id"] > plan.before["wrong_id"] or plan.after["correct"] < plan.before["correct"]:
        plan.verdict = "would_degrade"
        plan.detail = "the update would increase wrong IDs or lose correct matches on held-out verified captures"
    else:
        plan.verdict = "ok"
        plan.detail = "held-out verified captures: no more wrong IDs and no fewer correct matches"
    return plan
