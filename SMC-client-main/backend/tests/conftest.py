"""Shared test setup.

Environment is fixed BEFORE any app module is imported, so module-level
DB_PATH / DATA_DIR values point at a throwaway directory and a test run can
never touch the real backend/data database or a real camera.
"""

import os
import tempfile

_SESSION_DATA = tempfile.mkdtemp(prefix="smc-test-")
os.environ["DATA_DIR"] = _SESSION_DATA
for _n in (1, 2):
    os.environ["CAMERA_%d_URL" % _n] = ""
