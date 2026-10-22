"""
employee_directory.py

Single source of truth for employee_id -> (name, company), read from the
SMC `employees` table (face_db.py) — the same roster the Identity page and
face enrollment write to. Nothing is hardcoded: enrolling or renaming a
person on the Identity page is reflected here within CACHE_SECONDS.

Keyed by employee_id (never by name). Every other table (face_embeddings,
face_training_captures, the live overlay) already keys on that ID, so a
rename never detaches a person's enrollment photos or captures.

`company` is the employee's department (the roster has no company column).
"""

import threading
import time
from collections.abc import Mapping

CACHE_SECONDS = 5.0

# Every person box is drawn in this one colour (SMC spec). Identity and
# company never change it; only the label text differs.
PERSON_BOX_COLOR = "#2563EB"


class _DbEmployees(Mapping):
    """Read-only dict-like view of the employees table (id -> {name, company}),
    cached briefly so per-frame lookups don't hit the database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._loaded_at = 0.0

    def _load(self) -> dict[str, dict]:
        with self._lock:
            if time.monotonic() - self._loaded_at > CACHE_SECONDS:
                from app import face_db  # lazy: face_db must not import this module

                try:
                    rows = face_db.list_employees()
                    self._cache = {
                        str(r["employee_id"]): {"name": r["name"], "company": r.get("department") or ""} for r in rows
                    }
                except Exception:
                    # Keep serving the last good roster rather than dropping
                    # every name because of one failed read.
                    import logging

                    logging.getLogger("employee_directory").exception("could not read the employees table")
                self._loaded_at = time.monotonic()
            return self._cache

    def refresh(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def __getitem__(self, key: str) -> dict:
        return self._load()[key]

    def __iter__(self):
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


EMPLOYEES = _DbEmployees()


def get_employee(employee_id: str | None) -> dict | None:
    if not employee_id:
        return None
    return EMPLOYEES.get(str(employee_id))


def get_display(employee_id: str | None) -> tuple[str | None, str]:
    """Returns (name_or_None, box_color). name is None for an unrecognized
    person (caller shows "Person") or an employee_id not in the roster
    (caller falls back to showing the raw ID). The colour is always
    PERSON_BOX_COLOR."""
    emp = get_employee(employee_id)
    return (emp["name"] if emp else None), PERSON_BOX_COLOR
