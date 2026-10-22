"""Verified-only training material. Nothing here runs automatically, retrains a
model, or uses a prediction as a label: only captures a person approved or
corrected in Identity > Training & Captures are used.

    python -m scripts.verified_training dataset              # preview what the curated dataset would hold
    python -m scripts.verified_training dataset --apply      # write data/face_dataset/<employee_id>/ + manifest.csv
    python -m scripts.verified_training gallery              # dry run: would a gallery update help? (tested on held-out captures)
    python -m scripts.verified_training gallery --apply      # back the gallery up, then add the tested embeddings
    python -m scripts.verified_training rollback <backup.json>   # remove exactly the rows that update added
"""

from __future__ import annotations

import argparse
import json

from app import face_db, verified_training


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dataset")
    d.add_argument("--apply", action="store_true")
    g = sub.add_parser("gallery")
    g.add_argument("--apply", action="store_true")
    r = sub.add_parser("rollback")
    r.add_argument("backup")
    args = ap.parse_args(argv)

    face_db.init_face_tables()
    if args.cmd == "dataset":
        print(json.dumps(verified_training.build_dataset(apply=args.apply), indent=2))
    elif args.cmd == "gallery":
        if args.apply:
            print(json.dumps(verified_training.apply_gallery(), indent=2))
        else:
            plan, _ = verified_training.plan_gallery()
            print(json.dumps(verified_training.plan_public(plan), indent=2))
            print("(dry run: the gallery was not changed. Re-run with --apply to update it.)")
    else:
        print(f"removed {verified_training.rollback_gallery(args.backup)} gallery rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
