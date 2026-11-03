"""
gate_crossing.py

Entry-gate counting for intrusion zones marked as an entry gate (intrusion.py): only a person who
comes IN through the gate is marked; someone going out, standing on the line or walking past
outside never is. Same rules as the staff entrance line (staff/tracker.py), as pure logic with
the timestamps passed in, so it is unit-tested without a camera:

  - The gate is a line [[x1, y1], [x2, y2]] (fractions of the frame) and inside_sign says which
    side is inside (the side the editor's "Inside" arrow points to).
  - A person's position is the bottom-centre of their box (their feet). The signed distance to
    the line puts them "in", "out", or in a +/- BAND_PX band on the line (ignored).
  - Only points alongside the line count (within LINE_MARGIN of its ends), and inside the gate
    area when one is given, so someone walking well to the side doesn't take part.
  - A track's first confirmed side is only a starting point: someone first seen already inside
    did not come in. out -> in is an ENTRY; in -> out just updates the side (not marked).
  - A change of side is confirmed once the person has stayed on the new side CONFIRM_SECONDS, or
    disappeared while on it (they walked in and out of view). One entry per crossing; a new
    crossing by the same track needs it to have gone out again first.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.desk_tracker import in_polygon

BAND_PX = 20.0           # staff/config.py minimum_crossing_distance
LINE_MARGIN = 0.15       # staff/config.py line_margin
CONFIRM_SECONDS = 1.0    # staff/config.py crossing_confirm_seconds
LOST_SECONDS = 2.0       # unseen this long while pending "in" = walked in out of view
FORGET_SECONDS = 15.0    # unseen this long = forgotten


@dataclass
class _Track:
    side: str | None = None
    pending: str | None = None
    pending_since: float = 0.0
    last_seen: float = 0.0
    bbox: list | None = None


def side_of(foot, line, inside_sign: int, w: int, h: int) -> tuple[str, bool]:
    """(in | out | band, whether the point is alongside the line), in pixels."""
    (x1, y1), (x2, y2) = line
    ax, ay, bx, by = x1 * w, y1 * h, x2 * w, y2 * h
    abx, aby = bx - ax, by - ay
    length = (abx * abx + aby * aby) ** 0.5 or 1.0
    px, py = foot
    dist = (abx * (py - ay) - aby * (px - ax)) / length * (1 if inside_sign >= 0 else -1)
    t = ((px - ax) * abx + (py - ay) * aby) / (length * length)
    alongside = -LINE_MARGIN <= t <= 1 + LINE_MARGIN
    return ("in" if dist > BAND_PX else "out" if dist < -BAND_PX else "band"), alongside


class GateCounter:
    def __init__(self):
        self._tracks: dict[int, dict[int, _Track]] = {}     # zone id -> track id -> state

    def observe(self, zone_id: int, line, inside_sign: int, people: list[dict], w: int, h: int, ts: float,
                area: list | None = None) -> list[dict]:
        """people: [{track_id, bbox}] from the shared detector. Returns the people who just came in."""
        tracks = self._tracks.setdefault(zone_id, {})
        entries = []
        seen = set()
        for p in people:
            tid = p.get("track_id")
            if tid is None:
                continue
            b = p["bbox"]
            foot = ((b[0] + b[2]) / 2, b[3])
            if area and not in_polygon(foot[0] / w, foot[1] / h, area):
                continue
            where, alongside = side_of(foot, line, inside_sign, w, h)
            seen.add(tid)
            tr = tracks.setdefault(tid, _Track())
            tr.last_seen, tr.bbox = ts, b
            if where == "band" or not alongside:
                continue
            if tr.side is None:
                tr.side = where                    # a starting point, not a crossing
                continue
            if where == tr.side:
                tr.pending = None                  # came back before the change was confirmed
                continue
            if tr.pending != where:
                tr.pending, tr.pending_since = where, ts
            if ts - tr.pending_since >= CONFIRM_SECONDS:
                if self._confirm(tr):
                    entries.append({"track_id": tid, "bbox": b})
        for tid, tr in list(tracks.items()):
            if tid in seen:
                continue
            gone = ts - tr.last_seen
            if tr.pending and gone >= LOST_SECONDS:
                if self._confirm(tr):              # walked in and out of view
                    entries.append({"track_id": tid, "bbox": tr.bbox})
            if gone >= FORGET_SECONDS:
                del tracks[tid]
        return entries

    @staticmethod
    def _confirm(tr: _Track) -> bool:
        came_in = tr.side == "out" and tr.pending == "in"
        tr.side, tr.pending = tr.pending, None
        return came_in

    def reset_zone(self, zone_id: int) -> None:
        self._tracks.pop(zone_id, None)

    def keep_only(self, zone_ids: set[int]) -> None:
        for zid in [z for z in self._tracks if z not in zone_ids]:
            del self._tracks[zid]
