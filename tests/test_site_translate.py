"""POST /translate (site-translate): which site is asking, the WAF in prod,
the cache, the character budgets, protected tokens and privacy of the logs."""

import importlib.util
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import request_guard
from shared import site_translate as st
from shared import tenant as shared_tenant
from tenant_support import REGION, TENANT_A, TENANT_B, TENANT_TABLE, seed_tenancy, tenant_row

APP = Path(__file__).parents[1] / "functions" / "site-translate" / "app.py"
CACHE_TABLE = "test-site-translations"
GUARD_TABLE = "test-request-guard"
WAF = "waf-secret-value"
IP = "198.51.100.7"


class FakeTranslate:
    """Amazon Translate stand-in: "EN(<text>)" with the protected spans kept."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.mangle = False

    def translate_text(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["Text"]
        if text in self.fail:
            raise ClientError({"Error": {"Code": "ServiceUnavailableException"}}, "TranslateText")
        if self.mangle:
            text = text.replace('<span translate="no">', "").replace("</span>", "")
        return {"TranslatedText": f"{kwargs['TargetLanguageCode'].upper()}({text})"}


@pytest.fixture
def app(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION, "SITE_WAF_SECRET": WAF,
        "TRANSLATION_TABLE_NAME": CACHE_TABLE, "REQUEST_GUARD_TABLE_NAME": GUARD_TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch, tenant_b=tenant_row(TENANT_B, status="suspended"))
        ddb = boto3.resource("dynamodb", region_name=REGION)
        for name in (CACHE_TABLE, GUARD_TABLE):
            ddb.create_table(TableName=name, KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                             BillingMode="PAY_PER_REQUEST")
        tenants = ddb.Table(TENANT_TABLE)
        for host, tenant_id, status in (("anar.se", TENANT_A, "active"), ("new.anar.se", TENANT_A, "pending_dns"),
                                        ("other.se", TENANT_B, "active")):
            tenants.put_item(Item={"PK": f"DOMAIN#{host}", "SK": "TENANT", "tenantId": tenant_id})
            tenants.put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": f"DOMAIN#{host}", "status": status})
        tenants.put_item(Item={"PK": "SLUG#anar", "SK": "TENANT", "tenantId": TENANT_A})
        spec = importlib.util.spec_from_file_location("site_translate_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake = FakeTranslate()
        monkeypatch.setitem(st._clients, "translate", fake)
        monkeypatch.setattr(st, "_lru", st._Lru(100))
        monkeypatch.setattr(st, "BACKOFF_S", 0)
        # The Translate path is tested with an empty glossary; the reviewed
        # glossary has its own tests (test_site_translate_glossary.py).
        monkeypatch.setattr(st, "GLOSSARY", {})
        yield {"m": module, "fake": fake, "cache": ddb.Table(CACHE_TABLE), "guard": ddb.Table(GUARD_TABLE)}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


def call(app, payload, *, host="anar.se", waf=True, ip=IP, raw=None):
    headers = {"content-type": "application/json"}
    if waf:
        headers["x-booqy-waf"] = WAF
    if host:
        headers["x-booqy-viewer-host"] = host
        headers["x-booqy-viewer-ip"] = ip
    event = {"routeKey": "POST /translate", "headers": headers,
             "body": raw if raw is not None else json.dumps(payload),
             "requestContext": {"http": {"method": "POST", "path": "/translate", "sourceIp": "10.0.0.1"}}}
    response = app["m"].handler(event, None)
    return response["statusCode"], json.loads(response["body"]), response["headers"]


def test_translates_through_the_site_domain(app):
    status, body, headers = call(app, {"lang": "en", "texts": ["Boka bord", "  Välj datum "]})
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert body == {"lang": "en", "texts": ["EN(Boka bord)", "  EN(Välj datum) "], "partial": False}
    assert [c["SourceLanguageCode"] for c in app["fake"].calls] == ["sv", "sv"]


def test_spanish_is_informal_and_the_others_have_no_formality(app):
    call(app, {"lang": "es", "texts": ["Boka bord"]})
    call(app, {"lang": "ar", "texts": ["Boka bord"]})
    assert app["fake"].calls[0]["Settings"] == {"Formality": "INFORMAL"}
    assert "Settings" not in app["fake"].calls[1]


def test_second_request_is_served_from_the_cache(app):
    call(app, {"lang": "en", "texts": ["Boka bord"]})
    app["m"]._sites.clear()
    st._lru.items.clear()  # a cold container: only DynamoDB has it
    status, body, _ = call(app, {"lang": "fa", "texts": ["Boka bord"]})
    assert body["texts"] == ["FA(Boka bord)"]
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka bord", "Boka bord"]})
    assert body["texts"] == ["EN(Boka bord)"] * 2
    assert len(app["fake"].calls) == 2  # en once, fa once


def test_cache_rows_hold_no_source_text(app):
    call(app, {"lang": "en", "texts": ["Vår hemliga meny"]})
    rows = app["cache"].scan()["Items"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"PK", "text", "ttl"} and row["PK"].startswith("T#en#")
    assert "hemliga" not in row["PK"] and row["text"] == "EN(Vår hemliga meny)"
    assert int(row["ttl"]) > datetime.now(timezone.utc).timestamp() + 170 * 86400


def test_names_prices_and_times_are_protected(app):
    status, body, _ = call(app, {"lang": "en", "texts": ["Lägg till Kebab Shiraz för 245 kr kl 18:00"],
                                 "keep": ["Kebab Shiraz"]})
    sent = app["fake"].calls[0]["Text"]
    assert '<span translate="no">Kebab Shiraz</span>' in sent
    assert '<span translate="no">245 kr</span>' in sent and '<span translate="no">18:00</span>' in sent
    assert body["texts"] == ["EN(Lägg till Kebab Shiraz för 245 kr kl 18:00)"]


def test_a_text_without_free_words_is_never_sent(app):
    status, body, _ = call(app, {"lang": "en", "texts": ["Anar", "245 kr", "18:00-22:00"], "keep": ["Anar"]})
    assert body == {"lang": "en", "texts": ["Anar", "245 kr", "18:00-22:00"], "partial": False}
    assert app["fake"].calls == []


def test_a_translation_that_changed_a_token_stays_swedish_and_is_not_cached(app):
    app["fake"].mangle = True
    status, body, _ = call(app, {"lang": "en", "texts": ["Ring 08-123 45 67", "Boka"]})
    assert status == 200
    assert body["texts"] == ["Ring 08-123 45 67", "EN(Boka)"] and body["partial"] is True
    assert len(app["cache"].scan()["Items"]) == 1


def test_a_translate_failure_stays_swedish_and_gives_the_characters_back(app):
    app["fake"].fail.add("Trasig")
    status, body, _ = call(app, {"lang": "en", "texts": ["Trasig", "Hel"]})
    assert status == 200 and body["texts"] == ["Trasig", "EN(Hel)"] and body["partial"] is True
    assert len([c for c in app["fake"].calls if c["Text"] == "Trasig"]) == st.ATTEMPTS
    used = {row["PK"].split("#")[0]: int(row["used"]) for row in app["guard"].scan()["Items"] if "used" in row}
    assert used == {"tr-ip-chars": 3, "tr-tenant-chars": 3, "tr-global-chars": 3}


def test_no_translate_call_starts_after_the_deadline(app, monkeypatch):
    # A slow Translate must not run the Lambda (28 s) into a timeout: past
    # the deadline the remaining strings stay Swedish and are given back.
    monkeypatch.setattr(st, "DEADLINE_S", -1)
    status, body, _ = call(app, {"lang": "en", "texts": ["Sent", "Senare"]})
    assert status == 200 and body["texts"] == ["Sent", "Senare"] and body["partial"] is True
    assert app["fake"].calls == []
    used = {row["PK"].split("#")[0]: int(row["used"]) for row in app["guard"].scan()["Items"] if "used" in row}
    assert set(used.values()) <= {0}


def test_limit_env_names_match_terraform():
    # infrastructure site-translate/main.tf sets these names; a mismatch
    # silently falls back to the prod defaults in dev.
    source = APP.read_text()
    for name in ("SITE_TRANSLATE_IP_REQUESTS_PER_5MIN", "SITE_TRANSLATE_IP_CHARS_PER_HOUR",
                 "SITE_TRANSLATE_TENANT_CHARS_PER_30D", "SITE_TRANSLATE_GLOBAL_CHARS_PER_30D"):
        assert f'_limit("{name}"' in source


def test_over_a_budget_the_misses_stay_swedish_but_hits_are_served(app, monkeypatch):
    call(app, {"lang": "en", "texts": ["Boka bord"]})
    monkeypatch.setenv("SITE_TRANSLATE_TENANT_CHARS_PER_30D", "20")
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka bord", "En mycket lång text här"]})
    assert status == 200
    assert body == {"lang": "en", "texts": ["EN(Boka bord)", "En mycket lång text här"], "partial": True}
    assert len(app["fake"].calls) == 1
    # nothing was counted on the IP or global budgets for the refused request
    used = {row["PK"].split("#")[0]: int(row["used"]) for row in app["guard"].scan()["Items"] if "used" in row}
    assert used == {"tr-ip-chars": 9, "tr-tenant-chars": 9, "tr-global-chars": 9}


def test_the_ip_character_budget_is_per_ip(app, monkeypatch):
    monkeypatch.setenv("SITE_TRANSLATE_IP_CHARS_PER_HOUR", "10")
    assert call(app, {"lang": "en", "texts": ["Boka bord"]})[1]["partial"] is False
    assert call(app, {"lang": "en", "texts": ["Avboka"]})[1]["partial"] is True
    assert call(app, {"lang": "en", "texts": ["Avboka"]}, ip="198.51.100.8")[1]["partial"] is False


def test_requests_per_ip_are_limited(app, monkeypatch):
    monkeypatch.setenv("SITE_TRANSLATE_IP_REQUESTS_PER_5MIN", "2")
    assert call(app, {"lang": "en", "texts": ["A b"]})[0] == 200
    assert call(app, {"lang": "en", "texts": ["A b"]})[0] == 200
    status, body, headers = call(app, {"lang": "en", "texts": ["A b"]})
    assert status == 429 and body == {"error": "rate_limited"} and int(headers["Retry-After"]) >= 1


@pytest.mark.parametrize("host", ["unknown.se", "new.anar.se", "other.se", "bad host"])
def test_unknown_pending_or_inactive_sites_are_refused(app, host):
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka"]}, host=host)
    assert status == 403 and body == {"error": "unknown_site"}
    assert app["fake"].calls == []


def test_viewer_host_is_trusted_only_with_the_edge_secret(app):
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka"]}, waf=False)
    assert status == 403 and body == {"error": "unknown_site"}


def test_dev_direct_call_may_name_the_site_by_slug(app):
    assert call(app, {"lang": "en", "texts": ["Boka"], "slug": "anar"}, host=None, waf=False)[0] == 200
    assert call(app, {"lang": "en", "texts": ["Boka"], "slug": "nope"}, host=None, waf=False)[0] == 403
    # a cloudfront.net dev site comes through /api with a host nobody owns
    assert call(app, {"lang": "en", "texts": ["Boka"], "slug": "anar"}, host="d1.cloudfront.net")[0] == 200


def test_prod_needs_the_waf_and_rejects_slug(app, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert call(app, {"lang": "en", "texts": ["Boka"]})[0] == 200
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka"], "slug": "anar"}, host=None, waf=False)
    assert status == 400 and body == {"error": "invalid_request"}
    status, body, _ = call(app, {"lang": "en", "texts": ["Boka"]}, host=None, waf=False)
    assert status == 403 and body == {"error": "bot_check_failed"}


@pytest.mark.parametrize("payload, code", [
    ({"lang": "sv", "texts": ["Boka"]}, "unsupported_lang"),
    ({"lang": "de", "texts": ["Boka"]}, "unsupported_lang"),
    ({"lang": "en", "texts": []}, "invalid_request"),
    ({"lang": "en", "texts": ["Boka"], "host": "anar.se"}, "invalid_request"),
    ({"lang": "en", "texts": ["Boka"], "tenantId": TENANT_B}, "invalid_request"),
])
def test_bad_requests(app, payload, code):
    status, body, _ = call(app, payload)
    assert status == 400 and body == {"error": code}


def test_bad_json_and_wrong_route(app):
    assert call(app, None, raw="{nope")[0] == 400
    event = {"routeKey": "GET /translate", "headers": {}}
    assert app["m"].handler(event, None)["statusCode"] == 404


def test_logs_and_metrics_carry_no_text_and_no_ip(app, caplog, capsys):
    caplog.set_level(logging.INFO)  # what Lambda keeps (the app sets INFO)
    app["fake"].fail.add("Hemlig fras")
    call(app, {"lang": "en", "texts": ["Hemlig fras", "Annan rad"], "keep": ["Hemligt Namn"]})
    logged = caplog.text + capsys.readouterr().out
    for secret in ("Hemlig", "Annan", "Namn", IP):
        assert secret not in logged
    assert TENANT_A in logged and '"Misses": 2' in logged


def test_request_guard_consume_counts_and_refuses_over_the_budget(app):
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert request_guard.consume("k", "v", 6, 10, 3600, now) is True
    assert request_guard.consume("k", "v", 5, 10, 3600, now) is False
    assert request_guard.consume("k", "v", 4, 10, 3600, now) is True
    assert request_guard.consume("k", "v", 11, 10, 3600, now) is False
    request_guard.give_back("k", "v", 4, 3600, now)
    request_guard.give_back("k", "v", 99, 3600, now)  # never below zero
    rows = app["guard"].scan()["Items"]
    assert len(rows) == 1 and int(rows[0]["used"]) == 6 and "v" not in rows[0]["PK"].split("#")[1]


def test_request_guard_consume_fails_open(monkeypatch):
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    assert request_guard.consume("k", "v", 99, 1, 60, datetime.now(timezone.utc)) is True


# --- live test findings: glossary, the restaurant's own names, the IP budget ----------------

def _name_tenant(app, name, legal=None):
    item = {":n": name}
    expr = "SET #n = :n"
    if legal:
        expr += ", legalName = :l"
        item[":l"] = legal
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression=expr,
        ExpressionAttributeNames={"#n": "name"}, ExpressionAttributeValues=item)
    shared_tenant.reset_caches()


def test_glossary_texts_skip_translate_cache_and_budgets(app, monkeypatch):
    monkeypatch.setattr(st, "GLOSSARY", st.load_glossary())
    status, body, _ = call(app, {"lang": "es", "texts": ["Bekräfta bord", " Klart ", "Bord 2 hålls åt dig i 14:59",
                                                         "Välkommen till oss"]})
    assert status == 200 and body["partial"] is False
    assert body["texts"][:3] == ["Confirmar mesa", " Listo ", "Te guardamos la mesa 2 durante 14:59"]
    assert [c["Text"] for c in app["fake"].calls] == ["Välkommen till oss"]
    used = {row["PK"].split("#")[0]: int(row["used"]) for row in app["guard"].scan()["Items"] if "used" in row}
    assert used == {"tr-ip-chars": 18, "tr-tenant-chars": 18, "tr-global-chars": 18}
    assert len(app["cache"].scan()["Items"]) == 1


def test_glossary_wins_over_an_old_cached_translation(app, monkeypatch):
    call(app, {"lang": "en", "texts": ["Bekräfta bord"]})  # cached as Amazon's answer
    monkeypatch.setattr(st, "GLOSSARY", st.load_glossary())
    assert call(app, {"lang": "en", "texts": ["Bekräfta bord"]})[1]["texts"] == ["Confirm your table"]


def test_the_restaurants_own_names_are_protected_without_a_keep_list(app):
    _name_tenant(app, "ANAR Restaurang & Bar", legal="Anar Linköping AB")
    app["m"]._sites.clear()
    status, body, _ = call(app, {"lang": "ar", "texts": [
        "Catering från ANAR Restaurang & Bar i Linköping.", "Säljs av Anar Linköping AB", "ANAR RESTAURANG & BAR"]})
    sent = [c["Text"] for c in app["fake"].calls]
    assert '<span translate="no">ANAR Restaurang &amp; Bar</span>' in sent[0]
    assert '<span translate="no">Anar Linköping AB</span>' in sent[1]
    # a text that is only the name (here in capitals) is never sent
    assert body["texts"][2] == "ANAR RESTAURANG & BAR" and len(sent) == 2
    # the name comes back isolated inside the right-to-left sentence
    assert "⁨ANAR Restaurang & Bar⁩" in body["texts"][0]


def test_dish_names_from_the_keep_list_are_protected_in_catering_labels(app, monkeypatch):
    monkeypatch.setattr(st, "GLOSSARY", st.load_glossary())
    keep = ["Dagens fisk", "Burrata", "Grillad lax"]
    status, body, _ = call(app, {"lang": "ar", "keep": keep,
                                 "texts": ["Dagens fisk", "Portioner Burrata", "Grillad lax med dillstuvad potatis."]})
    assert body["texts"][0] == "Dagens fisk"
    assert body["texts"][1] == "عدد حصص ⁨Burrata⁩"
    assert [c["Text"] for c in app["fake"].calls] == [
        '<span translate="no">Grillad lax</span> med dillstuvad potatis.']
    assert body["texts"][2] == "AR(⁨Grillad lax⁩ med dillstuvad potatis.)"


def test_default_ip_budget_is_200000_characters_per_hour(app, monkeypatch):
    monkeypatch.delenv("SITE_TRANSLATE_IP_CHARS_PER_HOUR", raising=False)
    budgets = {kind: limit for kind, _, limit, _ in app["m"]._budgets(TENANT_A, IP)}
    assert budgets["tr-ip-chars"] == 200000


def test_cache_hits_do_not_count_on_the_ip_budget(app, monkeypatch):
    monkeypatch.setenv("SITE_TRANSLATE_IP_CHARS_PER_HOUR", "12")
    assert call(app, {"lang": "en", "texts": ["Boka bordet"]})[1]["partial"] is False
    for _ in range(3):
        assert call(app, {"lang": "en", "texts": ["Boka bordet"]})[1]["partial"] is False
    # a new text over what is left: stays Swedish and says so
    body = call(app, {"lang": "en", "texts": ["Boka bordet", "Ny text"]})[1]
    assert body == {"lang": "en", "texts": ["EN(Boka bordet)", "Ny text"], "partial": True}
