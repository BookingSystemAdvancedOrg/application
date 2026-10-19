"""shared.http.source_ip: trust CloudFront's viewer IP only with the edge secret."""

from shared import http


def ev(headers=None, ip="10.0.0.1"):
    return {"headers": headers or {}, "requestContext": {"http": {"sourceIp": ip}}}


def test_api_gateway_ip_without_secret(monkeypatch):
    monkeypatch.delenv("CLOUDFRONT_EDGE_SECRET", raising=False)
    assert http.source_ip(ev({"x-booqy-viewer-ip": "1.2.3.4", "x-booqy-edge": "s"})) == "10.0.0.1"


def test_viewer_ip_with_matching_secret(monkeypatch):
    monkeypatch.setenv("CLOUDFRONT_EDGE_SECRET", "s3cret")
    assert http.source_ip(ev({"X-Booqy-Viewer-Ip": "1.2.3.4", "x-booqy-edge": "s3cret"})) == "1.2.3.4"


def test_spoofed_viewer_ip_is_ignored(monkeypatch):
    monkeypatch.setenv("CLOUDFRONT_EDGE_SECRET", "s3cret")
    assert http.source_ip(ev({"x-booqy-viewer-ip": "1.2.3.4", "x-booqy-edge": "guess"})) == "10.0.0.1"
    assert http.source_ip(ev({"x-booqy-viewer-ip": "1.2.3.4"})) == "10.0.0.1"


def test_edge_header_needs_the_secret(monkeypatch):
    monkeypatch.setenv("CLOUDFRONT_EDGE_SECRET", "s3cret")
    good = ev({"x-booqy-viewer-host": "restaurant.booqy.se", "x-booqy-edge": "s3cret"})
    bad = ev({"x-booqy-viewer-host": "restaurant.booqy.se", "x-booqy-edge": "nope"})
    assert http.edge_header(good, "x-booqy-viewer-host") == "restaurant.booqy.se"
    assert http.edge_header(bad, "x-booqy-viewer-host") is None
