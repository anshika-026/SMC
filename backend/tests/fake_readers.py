"""Stand-ins for rtsp_reader.run, importable by a spawned child process."""

import time


def hang(url, fps, stop, seq, meta_q, settings=None):
    """A reader stuck inside FFmpeg: never produces a frame or a heartbeat,
    and ignores the stop event (only terminate/kill can end it)."""
    while True:
        time.sleep(0.5)


def exit_immediately(url, fps, stop, seq, meta_q, settings=None):
    """A reader that crashes on start."""
    raise SystemExit(3)


def start_real_reader_then_hang(pid_file):
    """Middle process: starts a REAL rtsp_reader against an unreachable
    camera, records the reader's pid, then waits to be killed abruptly."""
    import multiprocessing
    import pathlib

    from app import rtsp_reader

    ctx = multiprocessing.get_context("spawn")
    stop, seq, q = ctx.Event(), ctx.Value("Q", 0, lock=False), ctx.Queue()
    settings = {"connect_timeout": 1, "read_timeout": 1, "reconnect_delay": 0.5, "reconnect_max_delay": 1}
    p = ctx.Process(target=rtsp_reader.run, args=("rtsp://127.0.0.1:9/none", 5, stop, seq, q, settings), daemon=False)
    p.start()
    pathlib.Path(pid_file).write_text(str(p.pid))
    while True:
        time.sleep(1)


def record_url_then_hang(url, fps, stop, seq, meta_q, settings=None):
    """Writes the address this reader was asked to open (so a test can check which
    source a camera's reader got), then idles like a reader stuck in FFmpeg."""
    import os

    out = os.environ.get("FAKE_READER_URL_DIR")
    if out:
        import pathlib

        pathlib.Path(out, f"{os.getpid()}.url").write_text(url)
    while True:
        time.sleep(0.2)


def run_real_reader_on_fake_source(url, fps, stop, seq, meta_q, settings=None):
    """The REAL rtsp_reader.run (shared-memory publishing, status messages) reading
    from a scripted capture instead of a network stream: a live camera whose every
    frame is a 24x32 image of 7s. Used to prove decoded frames reach the pipeline."""
    import numpy as np

    from app import rtsp_reader

    class Cap:
        def isOpened(self):
            return True

        def grab(self):
            time.sleep(0.01)
            return True

        def retrieve(self):
            return True, np.full((24, 32, 3), 7, np.uint8)

        def get(self, prop):
            return 0.0

        def release(self):
            pass

    rtsp_reader.open_capture = lambda u, ct, rt: Cap()
    rtsp_reader.run(url, fps, stop, seq, meta_q, settings)
