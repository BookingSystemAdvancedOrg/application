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
import os

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


def read_stream(stream):
    """Text, tool uses and the stop reason from a ConverseStream event stream."""
    text, tools, current, stop = [], [], None, "end_turn"
    for event in stream:
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


class RealAgentModel(AgentModel):
    def __init__(self, client=None, model_id=None):
        self.model_id = model_id or os.environ.get("PHONE_AGENT_MODEL_ID") or ""
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", config=Config(
                connect_timeout=2, read_timeout=12, retries={"max_attempts": 2, "mode": "standard"}))
        return self._client

    def converse(self, system, messages, tools, *, max_tokens):
        if not self.model_id:
            raise AgentModelError("not_configured")
        args = {"modelId": self.model_id, "system": [{"text": system}], "messages": to_bedrock_messages(messages),
                "inferenceConfig": {"maxTokens": int(max_tokens)}}  # newer models refuse `temperature`
        tool_config = to_bedrock_tools(tools)
        if tool_config:
            args["toolConfig"] = tool_config
        try:
            answer = self.client.converse_stream(**args)
            return read_stream(answer.get("stream") or [])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise AgentModelError("unavailable" if code in RETRYABLE else "rejected", code in RETRYABLE) from None
        except BotoCoreError:
            raise AgentModelError("unavailable", True) from None
