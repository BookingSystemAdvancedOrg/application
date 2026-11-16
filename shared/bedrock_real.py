"""The real text model client: Amazon Bedrock Converse on BEDROCK_MODEL_ID
(a foundation model or EU inference profile id). One request, no streaming. With `json_schema` the model is told to answer with one JSON
object; the reply is returned with any code fence removed and checked to
parse, so callers can json.loads it.

`RealTextModel(timeouts=...)` gives the caller its own client with botocore
connect/read timeouts and standard retries (an interactive caller such as the
help assistant fails fast); without it the client keeps boto3's defaults.
The JSON instruction follows `language` ("sv" or "en").

Errors become TextModelError: "no_model" (empty BEDROCK_MODEL_ID),
"throttled" (retryable), "timeout" (connect or read timeout, retryable),
"unavailable" (retryable), "refused" (access or model not enabled),
"bad_output"."""

import json
import os

from shared.bedrock import TextModel, TextModelError

_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException",
              "ModelNotReadyException", "ModelTimeoutException"}
_JSON_INSTRUCTION = {
    "sv": "Svara med exakt ett JSON-objekt som följer detta JSON-schema, utan någon annan text:",
    "en": "Answer with exactly one JSON object that follows this JSON schema, with no other text:",
}
_client = None
_timed: dict = {}


def _bedrock(timeouts=None):
    """The default client, or one per (connect_timeout, read_timeout, max_attempts)."""
    global _client
    import boto3
    if timeouts is None:
        if _client is None:
            _client = boto3.client("bedrock-runtime")
        return _client
    key = (float(timeouts["connect_timeout"]), float(timeouts["read_timeout"]), int(timeouts["max_attempts"]))
    if key not in _timed:
        from botocore.config import Config
        _timed[key] = boto3.client("bedrock-runtime", config=Config(
            connect_timeout=key[0], read_timeout=key[1], retries={"mode": "standard", "max_attempts": key[2]}))
    return _timed[key]


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class RealTextModel(TextModel):
    def __init__(self, timeouts=None):
        """`timeouts`: {connect_timeout, read_timeout (seconds), max_attempts}, or None for boto3's defaults."""
        self.timeouts = timeouts

    def complete(self, system, prompt, *, max_tokens, json_schema=None, language="sv") -> str:
        model_id = os.environ.get("BEDROCK_MODEL_ID") or ""
        if not model_id:
            raise TextModelError("no_model")
        if json_schema is not None:
            system = (f"{system}\n\n{_JSON_INSTRUCTION.get(language, _JSON_INSTRUCTION['sv'])}\n"
                      f"{json.dumps(json_schema, ensure_ascii=False)}")
        from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError
        # Short, grounded answers: no extended thinking (Claude thinks by
        # default otherwise and can spend the whole budget on it).
        extra = {"additionalModelRequestFields": {"thinking": {"type": "disabled"}}} if "anthropic" in model_id else {}
        try:
            response = _bedrock(self.timeouts).converse(
                modelId=model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                # No temperature: current Claude models on Bedrock refuse it (deprecated).
                inferenceConfig={"maxTokens": int(max_tokens)},
                **extra,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ThrottlingException":
                raise TextModelError("throttled", True) from None
            if code in _RETRYABLE:
                raise TextModelError("unavailable", True) from None
            raise TextModelError("refused") from None
        except (ConnectTimeoutError, ReadTimeoutError):
            raise TextModelError("timeout", True) from None
        except BotoCoreError:
            raise TextModelError("unavailable", True) from None
        blocks = (((response.get("output") or {}).get("message") or {}).get("content")) or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if json_schema is None:
            return text.strip()
        text = _strip_fence(text)
        try:
            json.loads(text)
        except ValueError:
            # A sentence around the object: keep the outermost {...}.
            start, end = text.find("{"), text.rfind("}")
            try:
                text = text[start:end + 1] if 0 <= start < end else ""
                json.loads(text)
            except ValueError:
                raise TextModelError("bad_output") from None
        return text
