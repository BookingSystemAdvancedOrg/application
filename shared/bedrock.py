"""Text model port (CONTRACTS.md 13): Amazon Bedrock behind
`TextModel.complete`. `client()` picks the simulator or
shared/bedrock_real.py from BEDROCK_CLIENT (model id BEDROCK_MODEL_ID)."""

import json

from shared import external_client

SIM_TEXT = "Simulerad text"


class TextModelError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TextModel:
    def complete(self, system, prompt, *, max_tokens, json_schema=None) -> str:
        raise NotImplementedError


def smallest(schema):
    """The smallest instance valid against a (simple) JSON schema."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties") or {}
        return {k: smallest(props.get(k) or {}) for k in schema.get("required") or ()}
    if kind == "array":
        return [smallest(schema.get("items") or {}) for _ in range(int(schema.get("minItems") or 0))]
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    text = SIM_TEXT[:schema["maxLength"]] if "maxLength" in schema else SIM_TEXT
    return text.ljust(int(schema.get("minLength") or 0), ".")


class SimulatedTextModel(external_client.Simulated, TextModel):
    """Deterministic: canned `responses` first (in order), else a
    schema-shaped answer or "Simulerad text: <prompt start>"."""

    error = TextModelError

    def __init__(self, fail=None, responses=()):
        super().__init__(fail)
        self.responses = list(responses)

    def complete(self, system, prompt, *, max_tokens, json_schema=None):
        self._call("complete", prompt[:40])
        if self.responses:
            return self.responses.pop(0)
        if json_schema is not None:
            return json.dumps(smallest(json_schema), ensure_ascii=False)
        return f"{SIM_TEXT}: {prompt[:40]}"


def client(**kwargs) -> TextModel:
    if external_client.mode("bedrock") == external_client.REAL:
        from shared.bedrock_real import RealTextModel
        return RealTextModel()
    return SimulatedTextModel(**kwargs)
