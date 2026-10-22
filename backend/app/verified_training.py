"""
verified_training.py

Orchestration for the curated, human-verified training material:

  build_dataset(apply)        data/face_dataset/<employee_id>/ + manifest.csv,
                              from approved/corrected captures ONLY.
  plan_gallery()              dry run of an embedding-gallery update, tested on
                              held-out verified captures.
  apply_gallery()             the explicit update: backs the gallery up, adds the
                              planned embeddings, records their ids for rollback.
  rollback_gallery(path)      removes exactly the rows a given update added.

Nothing here retrains a model, and nothing runs automatically.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app import config, face_dataset, face_db, face_pipeline, storage

DATASET_DIR = os.environ.get("FACE_DATASET_DIR", str(Path(config.DATA_DIR) / "face_dataset"))
GALLERY_BACKUP_DIR = os.environ.get("FACE_GALLERY_BACKUP_DIR", str(Path(config.DATA_DIR) / "gallery_backups"))


def _roster_ids() -> set[str]:
    return {str(e["employee_id"]) for e in face_db.list_employees()}


def build_dataset(apply: bool) -> dict:
    rows = face_db.list_verified_captures()
    items, summary = face_dataset.build_verified_dataset(
        rows, _roster_ids(), storage.resolve, DATASET_DIR,
        min_blur=face_pipeline.MIN_BLUR_SCORE,
        min_brightness=face_pipeline.MIN_BRIGHTNESS,
        max_brightness=face_pipeline.MAX_BRIGHTNESS,
        apply=apply,
    )
    return {**summary, "applied": apply, "dataset_dir": DATASET_DIR}


def plan_gallery() -> tuple[face_dataset.GalleryPlan, dict]:
    rows = face_db.list_verified_captures()
    items, summary = face_dataset.build_verified_dataset(
        rows, _roster_ids(), storage.resolve, DATASET_DIR,
        min_blur=face_pipeline.MIN_BLUR_SCORE,
        min_brightness=face_pipeline.MIN_BRIGHTNESS,
        max_brightness=face_pipeline.MAX_BRIGHTNESS,
        apply=False,
    )
    kept = [i for i in items if i.decision == "kept"]
    plan = face_dataset.plan_gallery_update(kept, {r["id"]: r for r in rows}, face_db.get_all_embeddings(), face_pipeline.MATCH_THRESHOLD)
    return plan, summary


def plan_public(plan: face_dataset.GalleryPlan) -> dict:
    per: dict[str, int] = {}
    for who, *_ in plan.additions:
        per[who] = per.get(who, 0) + 1
    return {"verdict": plan.verdict, "detail": plan.detail, "would_add": len(plan.additions), "per_person": per,
            "held_out_tested": plan.held_out, "before": plan.before, "after": plan.after}


def apply_gallery() -> dict:
    """The explicit update. Refuses unless the plan tested 'ok'."""
    plan, _ = plan_gallery()
    out = plan_public(plan)
    if plan.verdict != "ok":
        return {**out, "applied": False, "reason": f"not applied: {plan.verdict}"}
    os.makedirs(GALLERY_BACKUP_DIR, exist_ok=True)
    path = os.path.join(GALLERY_BACKUP_DIR, f"gallery-{time.strftime('%Y%m%d-%H%M%S')}.json")
    backup = {"created_at": time.time(), "previous_gallery_rows": face_db.list_embedding_rows(), "added_ids": []}
    with open(path, "w", encoding="utf-8") as fh:  # the backup exists on disk BEFORE anything changes
        json.dump(backup, fh)
    ids = face_db.insert_embeddings([(w, e, p) for w, _c, e, p in plan.additions])
    backup["added_ids"] = ids
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(backup, fh)
    return {**out, "applied": True, "backup": path, "added_ids": ids}


def rollback_gallery(backup_path: str) -> int:
    with open(backup_path, encoding="utf-8") as fh:
        ids = json.load(fh).get("added_ids", [])
    return face_db.delete_embeddings(ids)
