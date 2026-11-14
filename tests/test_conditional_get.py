"""Conditional GET (shared/responses.conditional): a JSON 200 to a GET gets a
weak ETag, and the same ETag in If-None-Match gets an empty 304."""

from shared.responses import conditional, json_response, with_etag


def get(headers=None, method="GET"):
    return {"routeKey": f"{method} /locations/{{locationId}}/menu", "headers": headers or {}}


def test_get_200_gets_an_etag_and_no_cache():
    res = conditional(get(), json_response(200, {"items": [1, 2]}))
    assert res["statusCode"] == 200 and res["body"] == '{"items": [1, 2]}'
    assert res["headers"]["ETag"].startswith('W/"') and res["headers"]["Cache-Control"] == "private, no-cache"
    assert res["headers"]["Content-Type"] == "application/json"


def test_matching_if_none_match_is_an_empty_304():
    tag = conditional(get(), json_response(200, {"a": 1}))["headers"]["ETag"]
    for sent in (tag, tag.removeprefix("W/"), f'"other", {tag}'):
        res = conditional(get({"If-None-Match": sent}), json_response(200, {"a": 1}))
        assert res == {"statusCode": 304, "headers": {"ETag": tag, "Cache-Control": "private, no-cache"}, "body": ""}


def test_changed_body_is_a_full_200():
    tag = conditional(get(), json_response(200, {"a": 1}))["headers"]["ETag"]
    res = conditional(get({"if-none-match": tag}), json_response(200, {"a": 2}))
    assert res["statusCode"] == 200 and res["body"] == '{"a": 2}' and res["headers"]["ETag"] != tag


def test_only_plain_json_get_200s_are_touched():
    plain = json_response(200, {"a": 1})
    assert conditional(get(method="POST"), plain) is plain
    error = json_response(404, {"error": "not found"})
    assert conditional(get(), error) is error
    cookie = json_response(200, {"a": 1}, headers={"Set-Cookie": "x=1"})
    assert conditional(get(), cookie) is cookie
    binary = {**plain, "isBase64Encoded": True}
    assert conditional(get(), binary) is binary
    csv = {"statusCode": 200, "headers": {"Content-Type": "text/csv"}, "body": "a,b"}
    assert conditional(get(), csv) is csv
    assert conditional({"Records": []}, None) is None
    assert conditional(None, plain) is plain


def test_request_context_method_and_decorator():
    @with_etag
    def handler(event, context):
        return json_response(200, {"ok": True})

    event = {"requestContext": {"http": {"method": "GET"}}, "headers": {}}
    tag = handler(event, None)["headers"]["ETag"]
    assert handler({**event, "headers": {"If-None-Match": tag}}, None)["statusCode"] == 304
    assert handler.__name__ == "handler"


def test_blanket_no_store_from_the_respond_helpers_becomes_no_cache():
    # shared/http.respond (and the per-function respond helpers) put
    # Cache-Control: no-store on every answer; a GET JSON 200 still gets
    # its ETag, and the 304 works.
    from shared import http

    res = conditional(get(), http.respond(200, {"items": [1]}))
    assert res["headers"]["Cache-Control"] == "private, no-cache" and res["headers"]["ETag"].startswith('W/"')
    assert [k for k in res["headers"] if k.lower() == "cache-control"] == ["Cache-Control"]
    lower = json_response(200, {"a": 1}, headers={"cache-control": "no-store"})
    assert conditional(get(), lower)["headers"]["Cache-Control"] == "private, no-cache"
    assert "cache-control" not in conditional(get(), lower)["headers"]
    tag = res["headers"]["ETag"]
    again = conditional(get({"If-None-Match": tag}), http.respond(200, {"items": [1]}))
    assert again["statusCode"] == 304 and again["body"] == ""
    # anything else a handler chose stays as it is
    chosen = json_response(200, {"a": 1}, headers={"Cache-Control": "public, max-age=60"})
    assert conditional(get(), chosen) is chosen
    errors = http.respond(404, {"error": "not_found"})
    assert conditional(get(), errors) is errors
