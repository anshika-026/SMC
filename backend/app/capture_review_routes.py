"""
capture_review_routes.py

Identity > Training & Captures: browse the saved face captures by date, and let
a person approve, correct, mark unknown, or delete each one. The machine's
prediction (auto_*) is only ever shown, never turned into a label: only an
explicit approve / correct makes a capture trainable.

All routes are admin-only. Images are served by capture id (never by a
client-supplied path) and only from inside the face-training directory.

  GET  /api/faces/captures/dates               day -> counts
  GET  /api/faces/captures                     one page of captures (filters below)
  GET  /api/faces/captures/status              worker / camera / review status
  GET  /api/faces/captures/{id}                one capture's metadata
  GET  /api/faces/captures/{id}/image          the face crop (JPEG)
  POST /api/faces/captures/{id}/approve        "Yes - correct name" (predicted id)
  POST /api/faces/captures/{id}/correct        {employee_id}: a person's own correction
  POST /api/faces/captures/{id}/unknown        unknown / not suitable for training
  DELETE /api/faces/captures/{id}              delete (file removed, audit row kept)
  POST /api/faces/captures/bulk                {action, ids, employee_id?, confirm?}
  GET  /api/faces/captures/dataset/preview     what the verified-only dataset would contain
  POST /api/faces/captures/dataset/build       write data/face_dataset (verified only)
  GET  /api/faces/captures/gallery/plan        dry run of a gallery update (tested)
  POST /api/faces/captures/gallery/update      the explicit, backed-up gallery update
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import audit, auth, camera_db, config, face_collection, face_db, face_pipeline, storage, verified_training

log = logging.getLogger("capture_review")

router = APIRouter(prefix="/api/faces/captures", tags=["capture-review"], dependencies=[Depends(auth.require_admin)])

MAX_PAGE = 200
MAX_BULK = 500


def _reviewer(principal: dict) -> str:
    return principal.get("email") or principal.get("principal_type") or "admin"


def _roster() -> dict[str, str]:
    return {str(e["employee_id"]): e["name"] for e in face_db.list_employees()}


def _public(row: dict, names: dict[str, str] | None = None) -> dict:
    names = names if names is not None else _roster()
    cam = camera_db.get_camera(row["camera_id"])
    verified = row.get("verified_employee_id")
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "camera_name": cam["name"] if cam else f"Camera {row['camera_id']}",
        "track_id": row.get("track_id"),
        "captured_at": row["captured_at"],
        "predicted_employee_id": row.get("auto_employee_id"),
        "predicted_name": row.get("predicted_name") or names.get(row.get("auto_employee_id") or ""),
        "confidence": row.get("auto_confidence"),
        "prediction_tier": row.get("auto_tier"),
        "label_status": row["review_status"],  # pending_review | approved | corrected | unknown | rejected
        "verified_employee_id": verified,
        "verified_name": names.get(verified or ""),
        "training_eligible": bool(row.get("training_eligible")),
        "reviewed_at": row.get("reviewed_at"),
        "reviewed_by": row.get("reviewed_by"),
        "has_embedding": bool(row.get("has_embedding")),
        "quality": {"blur": row.get("blur_score"), "brightness": row.get("brightness"), "detection": row.get("detection_confidence")},
        "image_url": f"/api/faces/captures/{row['id']}/image",
    }


def _load(capture_id: int) -> dict:
    row = face_db.get_capture_for_review(capture_id)
    if row is None or row.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Capture not found")
    return row


def _check_employee(employee_id: str) -> str:
    employee_id = str(employee_id)
    if employee_id not in _roster():
        raise HTTPException(status_code=422, detail=f"'{employee_id}' is not one of the enrolled people")
    return employee_id


def _image_file(row: dict) -> Path | None:
    """The crop's file, but only if it lives inside the face-training folder."""
    p = storage.resolve(row.get("image_path"))
    if p is None or not p.is_file():
        return None
    root = Path(face_pipeline.TRAINING_CAPTURE_DIR).resolve()
    try:
        p.resolve().relative_to(root)
    except ValueError:
        return None
    return p


# --------------------------------------------------------------------------- listing

@router.get("/dates")
def capture_dates(camera_id: int | None = None, predicted: str | None = None, status: str | None = None):
    return {"dates": face_db.list_capture_dates(camera_id, predicted, status)}


@router.get("")
def list_captures(
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    camera_id: int | None = None,
    predicted: str | None = None,  # an employee id, or "unknown"
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=MAX_PAGE),
):
    if status and status not in face_db.REVIEW_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {face_db.REVIEW_STATUSES}")
    rows, total = face_db.query_captures(date, camera_id, predicted, status, page_size, (page - 1) * page_size)
    names = _roster()
    return {"total": total, "page": page, "page_size": page_size, "items": [_public(r, names) for r in rows]}


@router.get("/status")
def capture_status():
    """Is the capture worker running, are the cameras alive, how much is saved."""
    session = face_db.get_running_collection_session()
    now = time.time()
    active = set(face_collection.status()["active_camera_ids"])
    cameras = []
    for cam in camera_db.list_cameras():
        pipe = face_pipeline.get_existing_pipeline(cam["id"])
        overlay_age = (now - pipe.last_overlay_at) if pipe is not None and pipe.last_overlay_at else None
        cameras.append({
            "camera_id": cam["id"], "name": cam["name"], "collecting": cam["id"] in active,
            "recognition_running": pipe is not None,
            "overlay_age_seconds": round(overlay_age, 1) if overlay_age is not None else None,
            "process_ms": round(pipe.process_ms) if pipe is not None and pipe.process_ms is not None else None,
            "last_face_age_seconds": round(now - pipe.last_face_at, 1) if pipe is not None and pipe.last_face_at else None,
        })
    return {
        "worker": {
            "running": session is not None,
            "started_at": session["started_at"] if session else None,
            "planned_end_at": session["planned_end_at"] if session else None,
            "duplicates_rejected": session["duplicates_rejected"] if session else 0,
        },
        "cameras": cameras,
        "review_counts": face_db.count_review_status(),
        "disk_usage_bytes": face_collection.get_disk_usage_bytes(),
        "capture_dir": storage.to_stored(face_pipeline.TRAINING_UNLABELED_DIR),
        "limits": {"total": face_pipeline.MAX_TRAINING_CAPTURES, "per_camera": face_pipeline.MAX_CAPTURES_PER_CAMERA},
    }


@router.get("/{capture_id}")
def get_capture(capture_id: int):
    return _public(_load(capture_id))


@router.get("/{capture_id}/image")
def capture_image(capture_id: int):
    path = _image_file(_load(capture_id))
    if path is None:
        raise HTTPException(status_code=404, detail="Image file is missing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


# --------------------------------------------------------------------------- decisions

def _decide(capture_id: int, status: str, principal: dict, verified: str | None = None) -> dict:
    row = _load(capture_id)
    before = row["review_status"]
    updated = face_db.set_capture_review(capture_id, status, _reviewer(principal), verified)
    if updated is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    audit.record(f"capture.{status}", principal, target=f"capture:{capture_id}",
                 previous_status=before, predicted=row.get("auto_employee_id"),
                 predicted_confidence=row.get("auto_confidence"), verified=verified)
    return _public(updated)


@router.post("/{capture_id}/approve")
def approve(capture_id: int, principal: dict = Depends(auth.require_admin)):
    """'Yes - correct name': confirms the machine's prediction as the label."""
    row = _load(capture_id)
    predicted = row.get("auto_employee_id")
    if not predicted:
        raise HTTPException(status_code=422, detail="There is no predicted name to approve. Use Correct Identity or Unknown.")
    return _decide(capture_id, "approved", principal, _check_employee(predicted))


class CorrectIn(BaseModel):
    employee_id: str


@router.post("/{capture_id}/correct")
def correct(capture_id: int, body: CorrectIn, principal: dict = Depends(auth.require_admin)):
    return _decide(capture_id, "corrected", principal, _check_employee(body.employee_id))


@router.post("/{capture_id}/unknown")
def mark_unknown(capture_id: int, principal: dict = Depends(auth.require_admin)):
    return _decide(capture_id, "unknown", principal)


def _delete(capture_id: int, principal: dict) -> None:
    row = _load(capture_id)
    path = _image_file(row)
    # Database first: if the file removal then fails, the capture is already out
    # of the gallery and training, and the leftover file is only an orphan.
    if not face_db.mark_capture_deleted(capture_id, _reviewer(principal)):
        raise HTTPException(status_code=404, detail="Capture not found")
    if path is not None:
        try:
            os.remove(path)
        except OSError:
            log.exception("capture %s deleted in the database but its file could not be removed: %s", capture_id, path)
    audit.record("capture.delete", principal, target=f"capture:{capture_id}", previous_status=row["review_status"],
                 predicted=row.get("auto_employee_id"), file_removed=path is not None)


@router.delete("/{capture_id}")
def delete_capture(capture_id: int, principal: dict = Depends(auth.require_admin)):
    _delete(capture_id, principal)
    return {"ok": True, "id": capture_id}


class BulkIn(BaseModel):
    action: str = Field(pattern="^(approve|correct|unknown|delete)$")
    ids: list[int] = Field(min_length=1, max_length=MAX_BULK)
    employee_id: str | None = None
    confirm: bool = False


@router.post("/bulk")
def bulk(body: BulkIn, principal: dict = Depends(auth.require_admin)):
    if body.action in ("delete", "correct") and not body.confirm:
        raise HTTPException(status_code=422, detail=f"Bulk {body.action} needs confirm=true")
    if body.action == "correct":
        _check_employee(body.employee_id or "")
    done, failed = [], []
    for cid in dict.fromkeys(body.ids):
        try:
            if body.action == "approve":
                approve(cid, principal)
            elif body.action == "correct":
                correct(cid, CorrectIn(employee_id=body.employee_id), principal)
            elif body.action == "unknown":
                mark_unknown(cid, principal)
            else:
                _delete(cid, principal)
            done.append(cid)
        except HTTPException as e:
            failed.append({"id": cid, "error": e.detail})
    audit.record(f"capture.bulk_{body.action}", principal, requested=len(body.ids), done=len(done), failed=len(failed))
    return {"done": done, "failed": failed}


# --------------------------------------------------------------------------- verified dataset / gallery

@router.get("/dataset/preview")
def dataset_preview():
    return verified_training.build_dataset(apply=False)


@router.post("/dataset/build")
def dataset_build(principal: dict = Depends(auth.require_admin)):
    out = verified_training.build_dataset(apply=True)
    audit.record("dataset.build", principal, kept=out["kept"], excluded=out["excluded"])
    return out


@router.get("/gallery/plan")
def gallery_plan():
    plan, _ = verified_training.plan_gallery()
    return verified_training.plan_public(plan)


class GalleryUpdateIn(BaseModel):
    confirm: bool = False


@router.post("/gallery/update")
def gallery_update(body: GalleryUpdateIn, principal: dict = Depends(auth.require_admin)):
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Updating the gallery changes who is recognised: send confirm=true")
    out = verified_training.apply_gallery()
    audit.record("gallery.update", principal, applied=out.get("applied"), verdict=out.get("verdict"), added=len(out.get("added_ids", [])))
    return out


# --------------------------------------------------------------------------- capture worker control

class WorkerStartIn(BaseModel):
    camera_ids: list[int] | None = None  # default: every configured camera
    days: float | None = Field(None, gt=0, le=365)  # default: FACE_COLLECTION_DAYS (7)


@router.post("/worker/start")
def worker_start(body: WorkerStartIn = WorkerStartIn(), principal: dict = Depends(auth.require_admin)):
    """Starts (or extends) the capture session. Safe to call twice: a camera that
    is already being collected is not started again."""
    camera_ids = body.camera_ids or [c["id"] for c in camera_db.list_cameras() if c.get("is_configured")]
    if not camera_ids:
        raise HTTPException(status_code=422, detail="No configured cameras to capture from")
    session = face_collection.start_session(camera_ids, days=body.days)
    audit.record("capture.worker_start", principal, cameras=camera_ids, days=body.days)
    return {"ok": True, "session": session}


@router.post("/worker/stop")
def worker_stop(principal: dict = Depends(auth.require_admin)):
    """Stops capturing. Nothing already saved is deleted."""
    session = face_collection.stop_session(reason="stopped")
    audit.record("capture.worker_stop", principal)
    return {"ok": True, "session": session}
