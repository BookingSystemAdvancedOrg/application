"""The agent loop (ADR-L decision 2): one caller utterance in, one spoken
reply out. Builds the system prompt, sends the conversation and the tool
schemas to the `AgentModel` port, runs the tool uses inside Booqy
(`tools.Executor`), feeds the results back and returns the final text.
The voice provider never sees a tool definition, a tool result or tenant
data: it gets only the returned text."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date

from shared import agent_model
from shared.features.phone_agent import copy
from shared.features.phone_agent.tools import TOOLS

MAX_ROUNDS = 5       # model passes per caller utterance (tool calls in between)
MAX_TOKENS = 400


@dataclass
class TurnResult:
    reply: str
    tool_calls: list = field(default_factory=list)   # [{"name", "ok"}]
    latency_ms: int = 0
    error: bool = False


def system_prompt(lang, agent_name, restaurant, today: date) -> str:
    base = copy.SYSTEM_PROMPT[lang].format(agent=agent_name or copy.DEFAULT_AGENT_NAME,
                                           restaurant=restaurant or "restaurangen")
    return f"{base} Idag är det {today.isoformat()}." if lang == copy.SV else f"{base} Today is {today.isoformat()}."


def _ok(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is not False


def run_turn(model, messages: list, caller_text: str, executor, *, system: str) -> TurnResult:
    """Appends the caller's text and the agent's turns to `messages` (in
    place) and returns the reply. A model error ends the turn with the fixed
    error sentence (the caller is offered a callback)."""
    started = time.monotonic()
    messages.append({"role": "user", "content": caller_text})
    calls = []
    for _ in range(MAX_ROUNDS):
        try:
            turn = model.converse(system, messages, TOOLS, max_tokens=MAX_TOKENS)
        except agent_model.AgentModelError:
            lang = agent_model.language_of([caller_text])
            text = agent_model.CONFIRMATIONS["error"][0 if lang == "sv" else 1]
            messages.append({"role": "assistant", "content": text})
            return TurnResult(text, calls, int((time.monotonic() - started) * 1000), error=True)
        content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
            {"type": "tool_use", "id": u.id, "name": u.name, "input": dict(u.input)} for u in turn.tool_uses]
        messages.append({"role": "assistant", "content": content})
        if not turn.tool_uses:
            return TurnResult(turn.text or "", calls, int((time.monotonic() - started) * 1000))
        results = []
        for use in turn.tool_uses:
            result = executor.run(use.name, use.input)
            calls.append({"name": use.name, "ok": _ok(result)})
            results.append({"type": "tool_result", "tool_use_id": use.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str)})
        messages.append({"role": "user", "content": results})
    lang = agent_model.language_of([caller_text])
    text = agent_model.CONFIRMATIONS["error"][0 if lang == "sv" else 1]
    messages.append({"role": "assistant", "content": text})
    return TurnResult(text, calls, int((time.monotonic() - started) * 1000), error=True)


def dumps(messages) -> str:
    return json.dumps(messages, ensure_ascii=False, default=str)


def loads(value) -> list:
    try:
        out = json.loads(value or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []
