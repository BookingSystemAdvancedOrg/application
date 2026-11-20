"""Test paths for the voice gateway (picked up when pytest collects
services/voice-gateway/tests, also from the repo root in CI).

Root CI installs only the application's requirements-dev.txt, without
numpy, onnxruntime or amazon-transcribe: pure-logic tests run there; a
test that needs a runtime library starts with pytest.importorskip(...).
Test file names start with `test_vg_` (unique next to tests/ in the
default import mode)."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for path in (HERE, ROOT, ROOT / "tests", HERE / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def pytest_configure(config):
    config.addinivalue_line("markers", "aws: live tests against booqy-dev, opt-in with VG_AWS=1")
