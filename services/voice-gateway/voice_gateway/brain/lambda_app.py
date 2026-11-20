"""Read-only reuse of the phone-agent-call Lambda module. Owner: builder 'brain'.

functions/phone-agent-call/app.py holds the call-level glue the gateway
needs unchanged: start_call (number lookup, guards, refusal, greeting,
shared-number location question), finish (ended -> processed, metering,
cap warning), add_callback_for, sms_sender, _context, updated_or. Its
directory name has a hyphen, so it is loaded by path with importlib under
the module name `phone_agent_call_app`. The path is VG_APP_ROOT (the
application repo root; the Dockerfile sets /app) + functions/phone-agent-call/app.py,
else found relative to this package (services/voice-gateway/../..).

`load()` also installs GatewayVoice as the module's "voice" client
(app._clients["voice"]): start_call then neither says the greeting
(inline_speech) nor tries a provider transfer (deferred_tools: the
Executor returns transfer_to_number as a provider action that the
orchestrator executes on the TelephonyAdapter). GatewayVoice is
stateless, so one instance serves every call in the process.

FUTURE (minimal change, Stage 2 hardening): move start_call, finish and
run_call_turn into shared/features/phone_agent/line.py with `model` and
`voice` parameters; this module then becomes a plain import.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

MODULE_NAME = "phone_agent_call_app"


def app_root() -> Path:
    env = os.environ.get("VG_APP_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[4]


def load() -> ModuleType:
    """The phone-agent-call app module (cached in sys.modules), with
    GatewayVoice installed as its voice client."""
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    root = app_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    path = root / "functions" / "phone-agent-call" / "app.py"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    from voice_gateway.brain.voice_port import GatewayVoice
    module._clients["voice"] = GatewayVoice()
    return module
