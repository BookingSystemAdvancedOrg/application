"""Sending one marketing email (SES raw email with List-Unsubscribe).

Shared by marketing (test sends to the owner) and marketing-send
(campaigns). In dev, when MAIL_OUTBOX_BUCKET_NAME is set, the email is
written to the dev outbox (same format as the notification function's)
instead of going through SES, so the Console's "Mail outbox" shows it.

ENV_VARS:
    NO_REPLY_EMAIL_ADDRESS        SES From address (mail.<domain>)
    MARKETING_CONFIGURATION_SET   SES configuration set (bounce/complaint events)
    MAIL_OUTBOX_BUCKET_NAME       dev only, optional
"""

import json
import os
import uuid
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

import boto3

_clients = {}
_INVERT_MS = 10 ** 13 - 1


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def from_address(display_name):
    display = (display_name or "Restaurangen").replace('"', "").replace("\n", " ")[:60]
    return formataddr((display, os.environ["NO_REPLY_EMAIL_ADDRESS"]), charset="utf-8")


def _outbox_bucket():
    if os.environ.get("ENVIRONMENT") == "prod":
        return None
    return os.environ.get("MAIL_OUTBOX_BUCKET_NAME") or None


def build_mime(*, to, subject, text, html_body, sender, reply_to, unsubscribe_url):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = sender
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid(domain=os.environ.get("NO_REPLY_EMAIL_ADDRESS", "x@booqy.se").split("@")[-1])
    if unsubscribe_url:
        # RFC 2369 + RFC 8058: Gmail/Outlook show their own unsubscribe button
        # and POST to the link; required for bulk senders.
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send(*, to, subject, text, html_body, sender, reply_to=None, unsubscribe_url=None, kind="marketing"):
    """Sends one email. Returns the SES message id (or the outbox id in dev)."""
    bucket = _outbox_bucket()
    if bucket:
        now = datetime.now(timezone.utc)
        message_id = f"{_INVERT_MS - int(now.timestamp() * 1000):013d}-{uuid.uuid4().hex[:12]}"
        body = {"id": message_id, "at": now.isoformat().replace("+00:00", "Z"), "channel": "email",
                "kind": kind, "to": to, "from": sender, "replyTo": [reply_to] if reply_to else [],
                "subject": subject, "text": text, "html": html_body, "listUnsubscribe": unsubscribe_url}
        _client("s3").put_object(Bucket=bucket, Key=f"outbox/{message_id}.json",
                                 Body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 ContentType="application/json; charset=utf-8")
        return f"outbox-{message_id}"
    mime = build_mime(to=to, subject=subject, text=text, html_body=html_body, sender=sender,
                      reply_to=reply_to, unsubscribe_url=unsubscribe_url)
    params = {"Source": os.environ["NO_REPLY_EMAIL_ADDRESS"], "Destinations": [to],
              "RawMessage": {"Data": mime.as_bytes()}}
    config_set = os.environ.get("MARKETING_CONFIGURATION_SET")
    if config_set:
        params["ConfigurationSetName"] = config_set
    return _client("ses").send_raw_email(**params)["MessageId"]
