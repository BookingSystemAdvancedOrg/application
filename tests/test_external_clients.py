"""External client ports and simulators (CONTRACTS.md 13)."""

import json

import pytest

from shared import bedrock, external_client, fortnox, google_business, meta_graph, visma
from shared.fortnox import Voucher

CLIENTS = [  # (contract name, module, simulator class, real module, real class, error class)
    ("meta", meta_graph, meta_graph.SimulatedMetaGraph, "meta_graph_real", "RealMetaGraph",
     meta_graph.MetaGraphError),
    ("google_business", google_business, google_business.SimulatedGoogleBusiness, "google_business_real",
     "RealGoogleBusiness", google_business.GoogleBusinessError),
    ("fortnox", fortnox, fortnox.SimulatedFortnox, "fortnox_real", "RealFortnox", fortnox.AccountingProviderError),
    ("visma", visma, visma.SimulatedVisma, "visma_real", "RealVisma", fortnox.AccountingProviderError),
    ("bedrock", bedrock, bedrock.SimulatedTextModel, "bedrock_real", "RealTextModel", bedrock.TextModelError),
]
ENV = {"meta": "META_CLIENT", "google_business": "GOOGLE_BUSINESS_CLIENT", "fortnox": "FORTNOX_CLIENT",
       "visma": "VISMA_CLIENT", "bedrock": "BEDROCK_CLIENT"}


@pytest.mark.parametrize("name, module, sim, real_mod, real_cls, err", CLIENTS)
def test_factory_picks_simulator_by_default_and_when_asked(monkeypatch, name, module, sim, real_mod, real_cls, err):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv(ENV[name], raising=False)
    assert isinstance(module.client(), sim)
    monkeypatch.setenv(ENV[name], "simulator")
    assert isinstance(module.client(), sim)


@pytest.mark.parametrize("name, module, sim, real_mod, real_cls, err", CLIENTS)
def test_simulator_refused_in_prod(monkeypatch, name, module, sim, real_mod, real_cls, err):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv(ENV[name], "simulator")
    with pytest.raises(RuntimeError, match=f"{name} simulator is not allowed in prod"):
        module.client()


@pytest.mark.parametrize("name, module, sim, real_mod, real_cls, err", CLIENTS)
def test_unknown_value_refused(monkeypatch, name, module, sim, real_mod, real_cls, err):
    monkeypatch.setenv(ENV[name], "mock")
    with pytest.raises(ValueError):
        module.client()


@pytest.mark.parametrize("name, module, sim, real_mod, real_cls, err", [c for c in CLIENTS if c[0] != "bedrock"])
def test_real_is_a_skeleton(monkeypatch, name, module, sim, real_mod, real_cls, err):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv(ENV[name], "real")
    client = module.client()
    assert type(client).__name__ == real_cls
    with pytest.raises(NotImplementedError):
        client.revoke("t")


@pytest.mark.parametrize("name, module, sim, real_mod, real_cls, err", CLIENTS)
def test_fail_injection(name, module, sim, real_mod, real_cls, err):
    client = sim(fail={"complete" if name == "bedrock" else "revoke": 2})
    call = (lambda: client.complete("s", "p", max_tokens=5)) if name == "bedrock" else (lambda: client.revoke("t"))
    for _ in range(2):
        with pytest.raises(err) as exc:
            call()
        assert exc.value.retryable and exc.value.code == "unavailable"
    call()


def test_meta_is_idempotent_by_name_and_always_paused():
    meta = meta_graph.SimulatedMetaGraph()
    first = meta.create_campaign("t", "act_sim1", "Kampanj 1", "OUTCOME_TRAFFIC")
    assert meta.create_campaign("t", "act_sim1", "Kampanj 1", "OUTCOME_TRAFFIC") == first
    assert first.startswith("sim-campaign-") and len(first) == len("sim-campaign-") + 10
    assert meta.find_by_name("t", "act_sim1", "campaign", "Kampanj 1") == first
    assert meta.find_by_name("t", "act_sim1", "campaign", "Annan") is None
    assert meta.objects[first]["status"] == "PAUSED"
    assert meta.insights("t", "a", [first], "2026-11-01") == meta.insights("t", "a", [first], "2026-11-01")
    assert ("create_campaign", "Kampanj 1") in meta.calls


def test_meta_assets_and_missing():
    assets = meta_graph.SimulatedMetaGraph().list_assets("t")
    assert assets.ad_accounts[0]["currency"] == "SEK" and assets.ad_accounts[0]["has_payment_method"]
    assert meta_graph.SimulatedMetaGraph(missing=("ad_account",)).list_assets("t").ad_accounts == []


def test_meta_instagram_container_publishes_once():
    meta = meta_graph.SimulatedMetaGraph()
    container = meta.create_ig_container("t", "sim-ig-1", "https://x/a.jpg", "Hej")
    meta.publish_ig_container("t", "sim-ig-1", container)
    with pytest.raises(meta_graph.MetaGraphError) as exc:
        meta.publish_ig_container("t", "sim-ig-1", container)
    assert exc.value.code == "already_published"


def test_google_business_locations_and_hours():
    google = google_business.SimulatedGoogleBusiness()
    names = [loc["name"] for loc in google.list_locations("t")]
    assert names == ["locations/sim-1", "locations/sim-2"]
    google.patch_hours("t", "locations/sim-1", [{"day": "MONDAY"}], [])
    assert google.hours["locations/sim-1"]["regular"] == [{"day": "MONDAY"}]


@pytest.mark.parametrize("sim", [fortnox.SimulatedFortnox, visma.SimulatedVisma])
def test_accounting_refresh_rotates(sim):
    client = sim()
    token = client.exchange_code("simulated", "https://x/cb")
    newer = client.refresh(token.refresh_token)
    assert newer.refresh_token != token.refresh_token
    with pytest.raises(fortnox.AccountingProviderError) as exc:
        client.refresh(token.refresh_token)
    assert exc.value.code == "unauthorized"


def test_accounting_vouchers_numbered_per_series_and_findable():
    client = fortnox.SimulatedFortnox(missing_accounts={"2421"})
    rows = ({"account": "1930", "debit_ore": 100, "credit_ore": 0, "text": "x"},
            {"account": "3001", "debit_ore": 0, "credit_ore": 100, "text": "x"})
    a = client.post_voucher("t", Voucher("B", "2026-11-01", "Dag 2026-11-01 loc-1", rows))
    b = client.post_voucher("t", Voucher("B", "2026-11-02", "Dag 2026-11-02 loc-1", rows))
    assert (a.number, b.number) == (1, 2)
    assert client.find_voucher("t", "2026-11-01", "B", "loc-1") == a
    with pytest.raises(fortnox.AccountingProviderError) as exc:
        client.post_voucher("t", Voucher("B", "2026-11-03", "d",
                                         ({"account": "2421", "debit_ore": 1, "credit_ore": 0, "text": ""},
                                          {"account": "3001", "debit_ore": 0, "credit_ore": 1, "text": ""})))
    assert exc.value.code == "account_missing"


def test_bedrock_schema_shaped_output_and_canned_responses():
    model = bedrock.SimulatedTextModel(responses=["first"])
    assert model.complete("s", "p", max_tokens=10) == "first"
    schema = {"type": "object", "required": ["headline", "texts", "tone", "n"],
              "properties": {"headline": {"type": "string", "maxLength": 5},
                             "texts": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                             "tone": {"enum": ["varm", "kort"]}, "n": {"type": "integer", "minimum": 1},
                             "optional": {"type": "string"}}}
    assert json.loads(model.complete("s", "p", max_tokens=10, json_schema=schema)) == {
        "headline": "Simul", "texts": ["Simulerad text", "Simulerad text"], "tone": "varm", "n": 1}
    assert model.complete("s", "Skriv en annons om vår pizza", max_tokens=10) == \
        "Simulerad text: Skriv en annons om vår pizza"


def test_mode_reads_the_contract_env_var(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("FORTNOX_CLIENT", "real")
    assert external_client.mode("fortnox") == "real"


class _FakeConverse:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": self.error}}, "Converse")
        return {"output": {"message": {"content": [{"text": self.reply}]}}}


def test_real_bedrock_converse(monkeypatch):
    from shared import bedrock_real
    monkeypatch.setenv("BEDROCK_MODEL_ID", "eu.anthropic.example-model")
    fake = _FakeConverse('```json\n{"ok": true}\n```')
    monkeypatch.setattr(bedrock_real, "_client", fake)
    out = bedrock_real.RealTextModel().complete("sys", "fråga", max_tokens=50, json_schema={"type": "object"})
    assert json.loads(out) == {"ok": True}
    call = fake.calls[0]
    assert call["modelId"] == "eu.anthropic.example-model" and call["inferenceConfig"] == {"maxTokens": 50}
    assert call["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
    assert call["messages"] == [{"role": "user", "content": [{"text": "fråga"}]}]
    assert "JSON-schema" in call["system"][0]["text"]


@pytest.mark.parametrize("error, code, retryable", [("ThrottlingException", "throttled", True),
                                                    ("ServiceUnavailableException", "unavailable", True),
                                                    ("AccessDeniedException", "refused", False)])
def test_real_bedrock_errors(monkeypatch, error, code, retryable):
    from shared import bedrock_real
    monkeypatch.setenv("BEDROCK_MODEL_ID", "m")
    monkeypatch.setattr(bedrock_real, "_client", _FakeConverse(error=error))
    with pytest.raises(bedrock.TextModelError) as exc:
        bedrock_real.RealTextModel().complete("s", "p", max_tokens=5)
    assert (exc.value.code, exc.value.retryable) == (code, retryable)


def test_real_bedrock_refuses_empty_model_and_bad_json(monkeypatch):
    from shared import bedrock_real
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    with pytest.raises(bedrock.TextModelError, match="no_model"):
        bedrock_real.RealTextModel().complete("s", "p", max_tokens=5)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "m")
    monkeypatch.setattr(bedrock_real, "_client", _FakeConverse('Här är svaret: {"ok": true} hoppas det hjälper'))
    assert json.loads(bedrock_real.RealTextModel().complete("s", "p", max_tokens=5, json_schema={"type": "object"})) == {"ok": True}
    monkeypatch.setattr(bedrock_real, "_client", _FakeConverse("inte json"))
    with pytest.raises(bedrock.TextModelError, match="bad_output"):
        bedrock_real.RealTextModel().complete("s", "p", max_tokens=5, json_schema={"type": "object"})
