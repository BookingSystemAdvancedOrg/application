"""python -m voice_gateway.replay <scenario>: the replay harness CLI (tests/vg_harness/cli.py has the code and the
usage). Run it from the application root or from services/voice-gateway."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]           # services/voice-gateway
ROOT = HERE.parents[1]                               # the application repo root
for path in (ROOT / "tests", ROOT, HERE / "tests", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

if __name__ == "__main__":
    from vg_harness.cli import main
    sys.exit(main())
