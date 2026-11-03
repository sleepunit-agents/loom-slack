import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

# Every test's piece home is a temporary dir: nothing a test runs writes to the real one (~/.local/state).
import atexit  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_HOME = tempfile.mkdtemp(prefix="loom-venue-test-home-")
os.environ["LOOM_PIECE_HOME"] = _HOME
os.environ["HOME"] = _HOME     # a config made from an env of its own falls back to ~: this one
atexit.register(shutil.rmtree, _HOME, True)
