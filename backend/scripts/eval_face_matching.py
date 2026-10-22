"""Read-only evaluation of face matching against an app.db.

    python scripts/eval_face_matching.py path/to/app.db [--thresholds 0.3,0.35,...]

Scores every human-labelled CCTV capture that has an embedding
(face_training_captures, label_status='labeled') against the ENROLLED gallery
(face_embeddings) with the same code the live overlay uses
(app/face_matching.py), and reports:

  * genuine scores   - capture vs the gallery references of the person it was
                       labelled as,
  * impostor scores  - capture vs its best reference of any OTHER person,
  * per threshold: accepted-correct / accepted-wrong / rejected, and how many
    of the "accepted" ones the second-best margin would turn into ambiguity,
  * the same split by sharpness (blur_score) and detector confidence,
  * how many captures never produced an embedding at all, per camera.

The database is opened read-only; nothing is written. Output is aggregate
numbers only - no images, names or paths.
"""

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location(
    "face_matching", Path(__file__).resolve().parent.parent / "app" / "face_matching.py")
fm = importlib.util.module_from_spec(_spec)
sys.modules["face_matching"] = fm
_spec.loader.exec_module(fm)


def pct(a, b):
    return f"{100.0 * a / b:5.1f}%" if b else "  n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--thresholds", default="0.25,0.30,0.35,0.40,0.45,0.50,0.55")
    ap.add_argument("--margin", type=float, default=fm.MATCH_MARGIN)
    args = ap.parse_args()
    thresholds = [float(t) for t in args.thresholds.split(",")]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    gallery = [{"person_id": p, "embedding": json.loads(e)}
               for p, e in conn.execute("SELECT person_id, embedding FROM face_embeddings")]
    index = fm.build_index(gallery)
    print(f"gallery: {len(gallery)} rows -> {len(index)} usable refs, {len(set(index.person_ids))} people; "
          f"duplicates collapsed={index.duplicates_dropped} invalid={index.invalid_dropped} "
          f"cross-person conflicts={len(index.conflicts)}")
    for a, b, s in index.conflicts:
        print(f"   conflict: refs of {a} and {b} have similarity {s:.3f} (both refs excluded from matching)")

    print("\nno-embedding rate per camera (YOLO found a face, InsightFace produced nothing):")
    for cam, total, noemb in conn.execute(
            "SELECT camera_id, COUNT(*), SUM(label_status='no_embedding') FROM face_training_captures GROUP BY 1"):
        print(f"   camera {cam}: {noemb}/{total} = {pct(noemb, total)}")

    by_person = {}
    for pid in index.person_ids:
        by_person[pid] = by_person.get(pid, 0) + 1
    rows = conn.execute(
        "SELECT employee_id, embedding, blur_score, detection_confidence FROM face_training_captures "
        "WHERE label_status='labeled' AND embedding IS NOT NULL").fetchall()

    genuine, impostor_best, enrolled_samples = [], [], []
    unenrolled_best = []   # captures of people with NO enrolled reference: should always be rejected
    for emp, emb, blur, conf in rows:
        v = fm.clean_embedding(json.loads(emb))
        if v is None or len(index) == 0:
            continue
        sims = index.matrix @ v
        per = {}
        for pid, s in zip(index.person_ids, sims.tolist()):
            per[pid] = max(per.get(pid, -2.0), s)
        emp = str(emp)
        if emp in per:
            others = {p: s for p, s in per.items() if p != emp}
            g = per[emp]
            o = max(others.values()) if others else -1.0
            genuine.append(g)
            impostor_best.append(o)
            enrolled_samples.append((emp, g, o, blur, conf, sorted(per.values(), reverse=True)))
        else:
            unenrolled_best.append(max(per.values()))

    g = np.array(genuine)
    o = np.array(impostor_best)
    print(f"\nlabelled captures with an embedding: {len(rows)}; of enrolled people: {len(g)}; "
          f"of people with no enrolled photo: {len(unenrolled_best)}")
    if len(g):
        q = lambda a: " ".join(f"p{p}={np.percentile(a, p):.2f}" for p in (5, 25, 50, 75, 95))
        print(f"genuine score   {q(g)}")
        print(f"impostor (best other person) {q(o)}")
    if unenrolled_best:
        u = np.array(unenrolled_best)
        print(f"unenrolled people's best score {q(u)}")

    print("\nthreshold  correct  wrong-person  rejected(low)  | with margin rule: correct  wrong  ambiguous")
    for t in thresholds:
        ok = wrong = low = amb_ok = amb_wrong = ambiguous = c2 = w2 = 0
        for emp, gs, os_, _b, _c, ranked in enrolled_samples:
            best_is_true = gs >= os_
            top = max(gs, os_)
            if top < t:
                low += 1
                continue
            if best_is_true:
                ok += 1
            else:
                wrong += 1
            second = ranked[1] if len(ranked) > 1 else -1
            if second >= t and top - second < args.margin:
                ambiguous += 1
            elif best_is_true:
                c2 += 1
            else:
                w2 += 1
        n = len(enrolled_samples)
        fa_unenrolled = sum(1 for s in unenrolled_best if s >= t)
        print(f"  {t:.2f}    {pct(ok, n)}   {pct(wrong, n)}      {pct(low, n)}      |"
              f"   {pct(c2, n)}  {pct(w2, n)}  {pct(ambiguous, n)}"
              f"   unenrolled accepted: {fa_unenrolled}/{len(unenrolled_best)}")

    print("\ngenuine recall by sharpness (blur_score) at each threshold (accepted & correct):")
    edges = [(0, 50), (50, 150), (150, 400), (400, 1e9)]
    print("   blur band     n   " + "  ".join(f"@{t:.2f}" for t in thresholds))
    for lo, hi in edges:
        grp = [s for s in enrolled_samples if s[3] is not None and lo <= s[3] < hi]
        cells = [pct(sum(1 for s in grp if s[1] >= t and s[1] >= s[2]), len(grp)) for t in thresholds]
        print(f"   {lo:>4}-{hi if hi < 1e9 else 'inf':<5} {len(grp):5d}  " + "  ".join(cells))


if __name__ == "__main__":
    main()
