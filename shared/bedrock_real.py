"""The real text model client: Amazon Bedrock Converse on BEDROCK_MODEL_ID
(a foundation model or EU inference profile id). One request, no streaming. With `json_schema` the model is told to answer with one JSON
object; the reply is returned with any code fence removed and checked to
parse, so callers can json.loads it.

Errors become TextModelError: "no_model" (empty BEDROCK_MODEL_ID),
"throttled" (retryable), "unavailable" (retryable), "refused" (access or
model not enabled), "bad_output"."""

import json
import os

from shared.bedrock import TextModel, TextModelError

_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException",
              "ModelNotReadyException", "ModelTimeoutException"}
_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("bedrock-runtime")
    return _client


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class RealTextModel(TextModel):
    def complete(self, system, prompt, *, max_tokens, json_schema=None) -> str:
        model_id = os.environ.get("BEDROCK_MODEL_ID") or ""
        if not model_id:
            raise TextModelError("no_model")
        if json_schema is not None:
            system = (f"{system}\n\nSvara med exakt ett JSON-objekt som följer detta JSON-schema, "
                      f"utan någon annan text:\n{json.dumps(json_schema, ensure_ascii=False)}")
        from botocore.exceptions import BotoCoreError, ClientError
        # Short, grounded answers: no extended thinking (Claude thinks by
        # default otherwise and can spend the whole budget on it).
        extra = {"additionalModelRequestFields": {"thinking": {"type": "disabled"}}} if "anthropic" in model_id else {}
        try:
            response = _bedrock().converse(
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
