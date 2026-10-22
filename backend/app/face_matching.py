"""
face_matching.py

Identity matching of ONE live face embedding against the enrolled gallery
(face_db.face_embeddings), kept free of cv2/torch/insightface so it can be
unit-tested and reused by scripts/eval_face_matching.py with exactly the
code the live overlay runs.

What it adds over the loop it replaces in face_pipeline.py:
  - best score PER PERSON (still best single reference, not an average:
    an average of front + side + tilted references matches none of them well),
  - the second-best PERSON's score, so a face that fits two people about
    equally is refused (AMBIGUOUS_MATCH) rather than given to whichever
    happened to come first,
  - near-identical references of one person are collapsed (the same photo
    enrolled twice is still one reference, and cannot make that person look
    better supported than they are),
  - a reference that is (near-)identical to a reference of a DIFFERENT
    person is a data error (one photo filed under two people). Both people
    lose that reference for matching and it is reported, never silently
    resolved in favour of one of them. Nothing is written to the database.
  - an explicit rejection reason for every "no match" answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

# Rejection reasons (MatchResult.reason). None means a confirmed match.
INVALID_EMBEDDING = "INVALID_EMBEDDING"
NO_ENROLLED_REFERENCES = "NO_ENROLLED_REFERENCES"
BELOW_MATCH_THRESHOLD = "BELOW_MATCH_THRESHOLD"
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"

# Two references of ONE person closer than this are the same photo for
# matching purposes (enrolled twice, or re-encoded).
DUPLICATE_REFERENCE_SIM = float(os.environ.get("FACE_DUPLICATE_REFERENCE_SIM", "0.98"))
# References of DIFFERENT people this close cannot both be right.
CONFLICT_REFERENCE_SIM = float(os.environ.get("FACE_CONFLICT_REFERENCE_SIM", "0.90"))
# Second-best person only counts as a rival when it also clears the match
# threshold; then the winner must lead by at least this much.
MATCH_MARGIN = float(os.environ.get("FACE_MATCH_MARGIN", "0.05"))


@dataclass
class MatchResult:
    person_id: str | None = None          # set only for a confirmed match
    score: float = 0.0                     # best person's best-reference score
    best_person_id: str | None = None      # best candidate even when rejected (diagnostics)
    second_person_id: str | None = None
    second_score: float = 0.0
    margin: float = 0.0                    # score - second_score
    n_refs: int = 0                        # usable references of the best candidate
    reason: str | None = None


@dataclass
class GalleryIndex:
    matrix: np.ndarray                     # (n_refs, 512) float32, L2-normalised
    person_ids: list[str]                  # person id of each row
    n_input: int = 0
    duplicates_dropped: int = 0
    invalid_dropped: int = 0
    conflicts: list[tuple[str, str, float]] = field(default_factory=list)  # (person_a, person_b, sim)

    def __len__(self) -> int:
        return len(self.person_ids)

    def refs_for(self, person_id: str) -> int:
        return sum(1 for p in self.person_ids if p == person_id)


def clean_embedding(vec) -> np.ndarray | None:
    """float32 L2-normalised copy, or None if it is not a usable embedding
    (wrong shape, NaN/inf, zero or wildly non-unit norm)."""
    try:
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    if v.size == 0 or not np.isfinite(v).all():
        return None
    norm = float(np.linalg.norm(v))
    if norm < 1e-6 or abs(norm - 1.0) > 0.05:
        return None
    return v / norm


def build_index(entries: list[dict]) -> GalleryIndex:
    """entries: face_db.get_all_embeddings() rows [{person_id, embedding}]."""
    kept_vecs: list[np.ndarray] = []
    kept_ids: list[str] = []
    invalid = dup = 0
    expected_dim = None
    for e in entries:
        v = clean_embedding(e.get("embedding"))
        if v is None or (expected_dim is not None and v.size != expected_dim):
            invalid += 1
            continue
        expected_dim = v.size
        pid = str(e["person_id"])
        # Collapse a repeat of an already-kept reference of the SAME person.
        if any(p == pid and float(np.dot(v, k)) >= DUPLICATE_REFERENCE_SIM
               for p, k in zip(kept_ids, kept_vecs)):
            dup += 1
            continue
        kept_vecs.append(v)
        kept_ids.append(pid)

    conflicts: list[tuple[str, str, float]] = []
    drop: set[int] = set()
    if len(kept_vecs) > 1:
        m = np.stack(kept_vecs)
        sims = m @ m.T
        n = len(kept_ids)
        for i in range(n):
            for j in range(i + 1, n):
                if kept_ids[i] != kept_ids[j] and sims[i, j] >= CONFLICT_REFERENCE_SIM:
                    conflicts.append((kept_ids[i], kept_ids[j], float(sims[i, j])))
                    drop.update((i, j))
    keep = [i for i in range(len(kept_ids)) if i not in drop]
    matrix = (np.stack([kept_vecs[i] for i in keep]).astype(np.float32)
              if keep else np.zeros((0, expected_dim or 512), dtype=np.float32))
    return GalleryIndex(
        matrix=matrix,
        person_ids=[kept_ids[i] for i in keep],
        n_input=len(entries),
        duplicates_dropped=dup,
        invalid_dropped=invalid,
        conflicts=conflicts,
    )


def match(index: GalleryIndex, embedding, threshold: float, margin: float | None = None) -> MatchResult:
    """Identify one face. `threshold` is a cosine similarity (higher = closer)."""
    margin = MATCH_MARGIN if margin is None else margin
    v = clean_embedding(embedding)
    if v is None or (len(index) and v.size != index.matrix.shape[1]):
        return MatchResult(reason=INVALID_EMBEDDING)
    if len(index) == 0:
        return MatchResult(reason=NO_ENROLLED_REFERENCES)

    sims = index.matrix @ v
    best_per_person: dict[str, float] = {}
    for pid, s in zip(index.person_ids, sims.tolist()):
        if s > best_per_person.get(pid, -2.0):
            best_per_person[pid] = s
    ranked = sorted(best_per_person.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best = ranked[0]
    second_id, second = ranked[1] if len(ranked) > 1 else (None, 0.0)
    res = MatchResult(
        best_person_id=best_id, score=best,
        second_person_id=second_id, second_score=second,
        margin=best - second if second_id is not None else best,
        n_refs=index.refs_for(best_id),
    )
    if best < threshold:
        res.reason = BELOW_MATCH_THRESHOLD
    elif second_id is not None and second >= threshold and res.margin < margin:
        res.reason = AMBIGUOUS_MATCH
    else:
        res.person_id = best_id
    return res
