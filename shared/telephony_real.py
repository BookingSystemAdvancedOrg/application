"""The real telephony client: 46elks (ADR-L decision 4; Twilio stays the
documented alternative). Swedish numbers, one per enabled location; inbound
calls are connected to the voice provider's SIP target (ElevenLabs, TCP), and
when that leg fails or is busy the restaurant's own phone rings, so a Booqy or
ElevenLabs outage never means a dead number. The forwarding is a static
`voice_start` action on the number: 46elks runs it without calling Booqy.

Secret `${prefix}phone-agent/telephony` (TELEPHONY_SECRET_ARN, set by the
owner, JSON): `username` and `password` (the 46elks API credentials) and
`webhookToken` (a random string; only needed if the owner points a 46elks
callback at L13: `.../public/phone-agent/telephony/webhook?token=<it>`).

46elks has no webhook signature: callbacks are accepted only from its
published webhook addresses AND with the token (46elks.com/docs/ip-addresses).
Transfers are done by the voice provider (ElevenLabs transfer_to_number), so
`transfer` here answers unsupported.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

from shared.telephony import ForwardingCheck, Number, Telephony, TelephonyError, TelephonyEvent

API_BASE = "https://api.46elks.com/a1"
WEBHOOK_ADDRESSES = ("176.10.154.199", "85.24.146.132", "185.39.146.243", "2001:9b0:2:902::199")
TIMEOUT_S = 8


def _urllib_transport(method, url, headers, form):
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        **headers, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TelephonyError("unavailable", True) from None
    except ValueError:
        return 502, {}


def forward_action(sip_uri, fallback_e164):
    """The `voice_start` of a Booqy number: connect to the voice provider's SIP
    target; failed or busy rings the restaurant (when it has a number)."""
    action = {"connect": sip_uri}
    if fallback_e164:
        action.update({"failed": {"connect": fallback_e164}, "busy": {"connect": fallback_e164}})
    return action


class RealTelephony(Telephony):
    def __init__(self, *, secret=None, transport=None):
        self._secret = secret
        self._transport = transport or _urllib_transport
        self.base = os.environ.get("ELKS_API_BASE") or API_BASE

    @property
    def secret(self):
        if self._secret is None:
            arn = os.environ.get("TELEPHONY_SECRET_ARN")
            if not arn:
                raise TelephonyError("not_configured")
            try:
                raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
                self._secret = json.loads(raw)
            except Exception:  # noqa: BLE001 - never log the credentials
                raise TelephonyError("not_configured") from None
        return self._secret

    def _api(self, path, form=None, method="POST"):
        user, password = self.secret.get("username"), self.secret.get("password")
        if not user or not password:
            raise TelephonyError("not_configured")
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        status, body = self._transport(method, self.base + path, {"Authorization": auth}, form)
        if status == 404:
            raise TelephonyError("not_found")
        if status == 429 or status >= 500:
            raise TelephonyError("unavailable", True)
        if status not in (200, 201, 204):
            raise TelephonyError("number_unavailable" if path == "/numbers" else "rejected")
        return body or {}

    def provision_number(self, country="SE", label=""):
        if country != "SE":
            raise TelephonyError("unsupported_country")
        got = self._api("/numbers", {"country": "se", "capabilities": "voice", "name": str(label)[:60]})
        if not got.get("id") or not got.get("number"):
            raise TelephonyError("number_unavailable")
        return Number(got["number"], got["id"])

    def release_number(self, provider_id):
        try:
            self._api(f"/numbers/{urllib.parse.quote(str(provider_id))}", {"active": "no"})
        except TelephonyError as exc:
            if exc.code != "not_found":
                raise

    def set_inbound(self, provider_id, sip, fallback_e164):
        action = forward_action(sip.uri, fallback_e164)
        self._api(f"/numbers/{urllib.parse.quote(str(provider_id))}", {"voice_start": json.dumps(action)})

    def transfer(self, call_ref, to_e164, timeout_s):
        raise TelephonyError("unsupported")

    def parse_webhook(self, headers, body, *, source_ip=None, query=None, **request):
        token = str(self.secret.get("webhookToken") or "")
        given = str((query or {}).get("token") or "")
        allowed = tuple(x for x in os.environ.get("ELKS_WEBHOOK_ADDRESSES", "").split(",") if x) or WEBHOOK_ADDRESSES
        if not token or not hmac.compare_digest(given.encode(), token.encode()) or source_ip not in allowed:
            raise TelephonyError("unauthorized")
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
        form = {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        call_ref = form.get("callid") or form.get("id")
        if not call_ref:
            raise TelephonyError("unauthorized")
        if "duration" in form or "state" in form:      # whenhangup
            return [TelephonyEvent("ended", call_ref, {"to": form.get("to"), "duration_s": int(
                form.get("duration") or 0), "cause": form.get("state") or "hangup"})]
        return [TelephonyEvent("inbound", call_ref, {"from": form.get("from"), "to": form.get("to"),
                                                     "at": form.get("created")})]

    def verify_forwarding(self, e164):
        return ForwardingCheck("unsupported")
