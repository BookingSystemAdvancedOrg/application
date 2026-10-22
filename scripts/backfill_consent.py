#!/usr/bin/env python3
"""One-off: a consent log record for every subscriber who opted in before
the consent log existed.

    python scripts/backfill_consent.py --env dev            # dry run: counts only
    python scripts/backfill_consent.py --env dev --apply    # write the records

Run once per environment, right after the application release that adds
the consent log. Until then marketing-send skips every subscriber (no grant
on record means no send), so campaigns stay paused, never sent wrongly.

For each marketing subscriber row (TENANT#<t>#SUB) with status
"subscribed", one grant record for e-mail: source "legacy", the time and
source of the consent on the row, and the stored wording when the row has
one (welcome offer), otherwise the legacy wording for the form it came from
(booking or order/catering). Rows that are pending, unsubscribed, bounced or
complained get nothing. Idempotent: a subscriber that already has any record
is skipped, so a second run writes nothing.

Prints counts only, never addresses. Uses your current AWS credentials;
check the account first:
    aws sts get-caller-identity
"""

import argparse
import os
import sys

import boto3
from boto3.dynamodb.conditions import Attr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared import consent  # noqa: E402

_VERSION_BY_SOURCE = {"booking": "offers_booking_v0", "order": "offers_order_v0", "catering": "offers_order_v0",
                      "welcome_offer": "welcome_offer_v1"}


def plan(row):
    """(version, text, at) for one subscribed row, or None."""
    if row.get("status") != "subscribed" or not row.get("email"):
        return None
    version = row.get("consentVersion") if row.get("consentVersion") in consent.TEXTS else \
        _VERSION_BY_SOURCE.get(row.get("consentSource"), "legacy")
    text = row.get("consentText") or consent.render(version, row.get("restaurantName"))
    at = row.get("consentAt") or row.get("createdAt") or consent.now_iso()
    return version, text, at


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    prefix = "" if a.env == "prod" else f"{a.env}-"
    ddb = boto3.resource("dynamodb", region_name="eu-north-1")
    subs, log = ddb.Table(f"{prefix}marketing"), ddb.Table(f"{prefix}consent-log")
    counts = {"subscribed": 0, "alreadyLogged": 0, "written": 0}
    request = {"FilterExpression": Attr("SK").begins_with("SUB#") & Attr("status").eq("subscribed")}
    while True:
        page = subs.scan(**request)
        for row in page.get("Items", []):
            planned = plan(row)
            tenant_id = str(row["PK"]).split("#")[1]
            if not planned:
                continue
            counts["subscribed"] += 1
            if consent.state(log, tenant_id, consent.EMAIL, row["email"]) != "none":
                counts["alreadyLogged"] += 1
                continue
            version, text, at = planned
            if a.apply:
                item = consent._record(tenant_id, consent.EMAIL, row["email"], action=consent.GRANT,
                                       location_id=next(iter(row.get("locations") or []), None), source="legacy",
                                       ref=row.get("consentRef"), actor="backfill", version=version, text=text,
                                       at=at)
                consent._put(log, item)
            counts["written"] += 1
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(("APPLIED " if a.apply else "DRY RUN ") + " ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
