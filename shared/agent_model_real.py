"""The real agent model: Claude on Amazon Bedrock through the Converse
stream API (ADR-L decision 2), on PHONE_AGENT_MODEL_ID (a model id or an EU
inference profile id, e.g. `eu.anthropic.claude-...`). The stream is read to
the end here: the turn endpoint answers the voice provider with the whole
reply (ConverseStream needs only `bedrock:InvokeModelWithResponseStream`,
the one Bedrock action the role has).

Messages and tools are the port's (shared/agent_model.py); they are mapped
to Bedrock's content blocks here.
"""

from __future__ import annotations

import json
import math
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared.agent_model import AgentModel, AgentModelError, ToolUse, Turn

RETRYABLE = ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
             "InternalServerException", "ModelTimeoutException")


def _tool_result_content(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return [{"text": content or "-"}]
    return [{"json": content if isinstance(content, dict) else {"items": content}}]


def to_bedrock_messages(messages):
    """The port's messages as Bedrock Converse messages (roles alternate,
    content never empty)."""
    out = []
    for m in messages or []:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        converted = []
        for b in blocks:
            kind = b.get("type")
            if kind == "text" and str(b.get("text") or "").strip():
                converted.append({"text": b["text"]})
            elif kind == "tool_use":
                converted.append({"toolUse": {"toolUseId": b["id"], "name": b["name"], "input": b.get("input") or {}}})
            elif kind == "tool_result":
                converted.append({"toolResult": {"toolUseId": b["tool_use_id"],
                                                 "content": _tool_result_content(b.get("content")),
                                                 **({"status": "error"} if b.get("is_error") else {})}})
        if not converted:
            converted = [{"text": "..."}]
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(converted)
        else:
            out.append({"role": role, "content": converted})
    return out


def to_bedrock_tools(tools):
    specs = []
    for t in tools or []:
        t = t.get("toolSpec") or t
        specs.append({"toolSpec": {"name": t["name"], "description": t.get("description") or t["name"],
                                   "inputSchema": {"json": t.get("input_schema") or t.get("inputSchema", {}).get(
                                       "json") or {"type": "object", "properties": {}}}}})
    return {"tools": specs} if specs else None


def read_stream(stream, deadline=None):
    """Text, tool uses and the stop reason from a ConverseStream event stream
    (AgentModelError("timeout") once `deadline`, a time.monotonic(), passed)."""
    text, tools, current, stop = [], [], None, "end_turn"
    for event in stream:
        if deadline is not None and time.monotonic() > deadline:
            close = getattr(stream, "close", None)
            if close:
                close()
            raise AgentModelError("timeout", True)
        if "contentBlockStart" in event:
            start = (event["contentBlockStart"].get("start") or {}).get("toolUse")
            if start:
                current = {"id": start["toolUseId"], "name": start["name"], "input": []}
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                text.append(delta["text"])
            elif "toolUse" in delta and current is not None:
                current["input"].append(delta["toolUse"].get("input") or "")
        elif "contentBlockStop" in event and current is not None:
            raw = "".join(current["input"])
            try:
                args = json.loads(raw) if raw else {}
            except ValueError:
                args = {}
            tools.append(ToolUse(current["id"], current["name"], args if isinstance(args, dict) else {}))
            current = None
        elif "messageStop" in event:
            stop = event["messageStop"].get("stopReason") or stop
        elif any(k.endswith("Exception") for k in event):
            raise AgentModelError("unavailable", True)
    return Turn(("".join(text).strip() or None), tuple(tools), stop)


THINKING_HEADROOM = 1500
DEFAULT_THINKING = "low"


def thinking_fields():
    """(additionalModelRequestFields, extra max tokens) from PHONE_AGENT_THINKING.

    Claude Haiku 5.5 thinks by default (effort medium), and its reasoning
    counts against maxTokens: a hard language (Tigrinya) used every token on
    reasoning and answered nothing. The evaluation (scripts/phone_agent_eval.py)
    showed thinking off is about a second faster but reasons in the spoken
    text and skips read-backs, so the default is adaptive thinking at effort
    "low" with THINKING_HEADROOM tokens on top of the reply budget; "medium"
    or "high" think more, "off" disables thinking, "default" sends nothing."""
    value = (os.environ.get("PHONE_AGENT_THINKING") or DEFAULT_THINKING).lower()
    if value == "off":
        return {"thinking": {"type": "disabled"}}, 0
    if value in ("low", "medium", "high"):
        return {"output_config": {"effort": value}}, THINKING_HEADROOM
    return None, THINKING_HEADROOM


READ_TIMEOUT_S = 12          # without a turn deadline (a direct call)
CONNECT_TIMEOUT_S = 2
MIN_ATTEMPT_S = 2.5          # a retry is sent only when this much of the turn is left


class RealAgentModel(AgentModel):
    """`timeout_s` (the time left of the caller's turn) sets the connect and
    read timeouts of the call, bounds reading the stream, and allows the one
    retry of a throttled or failed call only when MIN_ATTEMPT_S still fits;
    past it AgentModelError("timeout") (agent.run_turn: the still-checking
    sentence). Botocore never retries on its own (it would not know the
    deadline)."""

    def __init__(self, client=None, model_id=None):
        self.model_id = model_id or os.environ.get("PHONE_AGENT_MODEL_ID") or ""
        self._client = client
        self._clients = {}

    @property
    def client(self):
        return self.client_for(None)

    def client_for(self, seconds):
        """A Bedrock client whose timeouts fit `seconds` (whole seconds, so a
        container keeps a handful), or the injected one."""
        if self._client is not None:
            return self._client
        read = READ_TIMEOUT_S if seconds is None else max(1, min(READ_TIMEOUT_S, math.ceil(seconds)))
        if read not in self._clients:
            self._clients[read] = boto3.client("bedrock-runtime", config=Config(
                connect_timeout=min(CONNECT_TIMEOUT_S, read), read_timeout=read,
                retries={"total_max_attempts": 1, "mode": "standard"}))
        return self._clients[read]

    def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
        if not self.model_id:
            raise AgentModelError("not_configured")
        args = {"modelId": self.model_id, "system": [{"text": system}], "messages": to_bedrock_messages(messages),
                "inferenceConfig": {"maxTokens": int(max_tokens)}}  # newer models refuse `temperature`
        extra, headroom = thinking_fields()
        if extra:
            args["additionalModelRequestFields"] = extra
            args["inferenceConfig"]["maxTokens"] = int(max_tokens) + headroom
        tool_config = to_bedrock_tools(tools)
        if tool_config:
            args["toolConfig"] = tool_config
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        for attempt in range(2):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise AgentModelError("timeout", True)
            try:
                return self._once(args, left, deadline)
            except AgentModelError as exc:
                fits = left is None or deadline - time.monotonic() >= MIN_ATTEMPT_S
                if attempt or not exc.retryable or not fits:
                    if exc.retryable and not fits:
                        raise AgentModelError("timeout", True) from None
                    raise

    def _once(self, args, left, deadline):
        try:
            answer = self.client_for(left).converse_stream(**args)
            return read_stream(answer.get("stream") or [], deadline)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise AgentModelError("unavailable" if code in RETRYABLE else "rejected", code in RETRYABLE) from None
        except BotoCoreError:
            raise AgentModelError("unavailable", True) from None
