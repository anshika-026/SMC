"""
heap.py

Keeps Python's cyclic garbage collector from freezing the server.

The backend holds millions of long-lived Python objects (PyTorch, Ultralytics,
InsightFace, transformers and their models). A full collection walks all of them
while every thread - including the one serving the API and websockets - waits.
On a busy 8-core laptop one such pass was caught (with a stack dump) blocking the
API for ~25 s, so the portal showed no cameras.

Objects that live for the whole run do not need collecting. gc.freeze() moves
everything currently tracked into a permanent generation the collector skips;
later collections only look at objects created afterwards. It is called once
the models are loaded (startup, first pipeline) and again a few minutes into the
run for anything loaded lazily. Long collections are logged so this stays visible.
"""

from __future__ import annotations

import gc
import logging
import threading
import time

log = logging.getLogger("heap")

SLOW_GC_SECONDS = 1.0
_started: dict[int, float] = {}


def freeze(reason: str) -> int:
    """Exclude every object alive now from future collections; returns how many
    objects are frozen in total."""
    gc.freeze()
    n = gc.get_freeze_count()
    log.info("heap frozen (%s): %d long-lived objects excluded from garbage collection", reason, n)
    return n


def _gc_callback(phase: str, info: dict) -> None:
    gen = info.get("generation")
    if phase == "start":
        _started[gen] = time.perf_counter()
    elif phase == "stop" and gen in _started:
        took = time.perf_counter() - _started.pop(gen)
        if took >= SLOW_GC_SECONDS:
            log.warning("garbage collection (generation %s) blocked all threads for %.1fs", gen, took)


def install(refreeze_after=(60.0, 180.0, 600.0)) -> None:
    """Log slow collections and re-freeze a few times in the background (models
    and pipelines load lazily after startup)."""
    if _gc_callback not in gc.callbacks:
        gc.callbacks.append(_gc_callback)

    def _later():
        start = time.monotonic()
        for delay in refreeze_after:
            time.sleep(max(0.0, start + delay - time.monotonic()))
            freeze(f"{int(delay)}s after start")

    threading.Thread(target=_later, name="heap-refreeze", daemon=True).start()
