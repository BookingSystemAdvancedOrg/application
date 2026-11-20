"""Entry point. Owner: builder 'brain'.

  python -m voice_gateway.server local --caller caller.wav --out call.wav [--fake] [--dialed +46...]
      one call over LocalAdapter: the WAV is the caller (played once, then
      silence until the agent ends or 60 s), the stereo recording and a
      metrics JSON are written next to --out; --fake uses FakeSttFactory,
      FakeTts and a scripted brain (no AWS).
  python -m voice_gateway.server ws [--port 8765]
      WebSocketAdapter on 127.0.0.1 for manual tests.
  python -m voice_gateway.server serve
      Fargate (later, DESIGN.md 10): config from SSM, /health on :8080,
      the AWS phone adapter.
Process start: GatewayConfig, ClipCache.warm for every languages.SPOKEN
code with a token or filler (never blocks accepting calls).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
