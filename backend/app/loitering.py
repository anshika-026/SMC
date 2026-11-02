"""
loitering.py

Dwell-time tracking for loitering ("standing") zones. Pure logic with an injectable clock, so
it is unit-tested without a camera: intrusion.py feeds it, once per detector cycle, which
tracked people are standing inside each loitering zone.

Lifecycle of one person (track ID) in one zone of one camera:

  enter      first cycle the track is inside      -> a visit starts, timer = 0
  inside     seen inside again                    -> timer keeps running
  missed     not inside on a cycle                -> a gap starts; the timer keeps running
  back       inside again within the grace period -> same visit (an occlusion or a missed detection)
  left       absent for MORE than grace seconds   -> visit forgotten; a later entry is a new visit
  dwell      timer reaches the zone's duration    -> ONE alert for the visit, never again for it
  re-arm     the visit is forgotten (left)        -> the next visit can alert again

Timers are per camera, per zone and per track ID, so two people, or the same person in two
zones, never share one. Elapsed time uses a monotonic clock (immune to wall-clock changes).
If a camera delivers nothing for STALE_SECONDS (it dropped and came back) everything on that
camera is forgotten rather than guessed at. A track the tracker loses and re-issues under a new
ID starts a new visit; the grace period only covers the same ID reappearing.
"""

import os
import time
from dataclasses import dataclass

DEFAULT_GRACE_SECONDS = float(os.environ.get("LOITER_GRACE_SECONDS", "10"))
STALE_SECONDS = float(os.environ.get("LOITER_STALE_SECONDS", "60"))


@dataclass
class _Visit:
    entered: float
    absent_since: float | None = None
    alerted: bool = False


class LoiterTracker:
    def __init__(self, clock=time.monotonic, stale_seconds: float = STALE_SECONDS):
        self._clock = clock
        self._stale = stale_seconds
        self._visits: dict[tuple[int, int], dict[int, _Visit]] = {}   # (camera, zone) -> track -> visit
        self._last_call: dict[int, float] = {}                        # camera -> last observe()

    def observe(self, camera_id: int, zones: list[tuple[int, float, float, set]]) -> list[tuple[int, int, float]]:
        """zones: [(zone_id, dwell_seconds, grace_seconds, track_ids_inside_now)].
        Returns [(zone_id, track_id, seconds_inside)] for visits that JUST reached their duration."""
        now = self._clock()
        last = self._last_call.get(camera_id)
        if last is not None and now - last > self._stale:
            self._drop_camera(camera_id)
        self._last_call[camera_id] = now

        fired = []
        for zone_id, dwell, grace, inside in zones:
            visits = self._visits.setdefault((camera_id, zone_id), {})
            for tid in inside:
                v = visits.get(tid)
                if v is not None and v.absent_since is not None and now - v.absent_since > grace:
                    v = None                          # it had been gone longer than grace: a new visit
                if v is None:
                    visits[tid] = v = _Visit(entered=now)
                v.absent_since = None
                if not v.alerted and now - v.entered >= dwell:
                    v.alerted = True
                    fired.append((zone_id, tid, now - v.entered))
            for tid in [t for t in visits if t not in inside]:
                v = visits[tid]
                if v.absent_since is None:
                    v.absent_since = now
                elif now - v.absent_since > grace:
                    del visits[tid]                   # left: forgotten, and the next visit re-arms
        return fired

    def seconds_inside(self, camera_id: int, zone_id: int, track_id: int) -> float | None:
        v = self._visits.get((camera_id, zone_id), {}).get(track_id)
        return None if v is None else self._clock() - v.entered

    def reset_zone(self, zone_id: int) -> None:
        """Zone edited or removed: drop its timers so a changed outline/duration starts clean."""
        for key in [k for k in self._visits if k[1] == zone_id]:
            del self._visits[key]

    def keep_only(self, zone_ids: set[int]) -> None:
        for key in [k for k in self._visits if k[1] not in zone_ids]:
            del self._visits[key]

    def _drop_camera(self, camera_id: int) -> None:
        for key in [k for k in self._visits if k[0] == camera_id]:
            del self._visits[key]
