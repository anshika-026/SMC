"""Shared event-clip recorder (clips.py): rolling buffer per wanted camera,
~5 s before + ~5 s after, real H.264 encode, and its failure cases."""

import time

import numpy as np

from app import clips


def _fill(rec, camera_id, t0, seconds=12.0, fps=6):
    """Puts real JPEG frames into the buffer, spread around t0."""
    for i in range(int(seconds * fps)):
        ts = t0 - 6 + i / fps
        frame = np.full((360, 640, 3), (i * 3) % 255, np.uint8)
        rec._buffer_frame(camera_id, frame, ts)


def test_only_wanted_cameras_are_buffered():
    rec = clips.ClipRecorder()
    rec.want("footfall", {4})
    rec.want("visitors", {4, 5})
    assert rec.is_wanted(4) and rec.is_wanted(5) and not rec.is_wanted(1)
    _fill(rec, 1, time.time())
    assert rec.frames(1, time.time()) == []
    rec.want("visitors", set())                 # footfall still wants camera 4
    assert rec.is_wanted(4) and not rec.is_wanted(5)


def test_writes_a_playable_clip_around_the_event(tmp_path):
    rec = clips.ClipRecorder()
    rec.want("footfall", {4})
    t0 = time.time()
    _fill(rec, 4, t0)
    got = []
    final = tmp_path / "footfall_clips" / "4" / "person_1_1.mp4"
    rec._write(4, t0, final, lambda status, frames=None, error=None: got.append((status, frames, error)), max_dir_mb=100)
    assert got[0][0] == "ready" and got[0][1] >= 50     # ~10 s at 6 fps
    assert final.is_file() and final.stat().st_size > 1000
    assert final.read_bytes()[4:8] == b"ftyp"            # an MP4
    import av

    with av.open(str(final)) as c:
        s = c.streams.video[0]
        assert s.codec_context.name == "h264"
        assert 8 <= float(s.frames / s.average_rate) <= 12   # about 10 seconds


def test_no_video_and_full_disk_are_reported_not_raised(tmp_path):
    rec = clips.ClipRecorder()
    rec.want("footfall", {4})
    got = []
    final = tmp_path / "clips" / "4" / "x.mp4"
    rec._write(4, time.time(), final, lambda status, frames=None, error=None: got.append((status, error)), None)
    assert got[-1] == ("failed", clips.NO_VIDEO)
    _fill(rec, 4, time.time())
    (tmp_path / "clips" / "4").mkdir(parents=True)
    (tmp_path / "clips" / "4" / "big.bin").write_bytes(b"\x00" * 2_000_000)
    rec._write(4, time.time(), final, lambda status, frames=None, error=None: got.append((status, error)), max_dir_mb=1)
    assert got[-1] == ("failed", clips.DISK_FULL) and not final.exists()
