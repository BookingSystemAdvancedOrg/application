#!/usr/bin/env python3
"""One-off: copy today's menu dishes into the catering menu (issue 5 M1).

    python scripts/migrate_catering_menu.py --env dev            # dry run: counts only
    python scripts/migrate_catering_menu.py --env dev --apply    # write the dishes

For every location with catering switched on (cateringSettings.enabled), each
active, non-alcohol MENU# item becomes a catering dish (DISH#) with the same
name, description and price (pricePerPortion, kronor), no allergens or
dietary labels (the menu has none; the owner adds them in the admin), active,
and `sourceMenuItemId`. It is a copy: later menu edits do not reach it.

Idempotent: the dish id is derived from the location and menu item id and
written only if missing, so a second run (or a dish the owner already edited)
is skipped. Nothing is ever deleted or overwritten.

Prints counts only. Uses your current AWS credentials; check the account
first:
    aws sts get-caller-identity
"""

import argparse
import os
import sys
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared import catering as c  # noqa: E402

_NAMESPACE = uuid.UUID("6f1c3c2e-6c55-4f6e-9d2b-3b0c7c1e5a10")


def dish_id(location_id, menu_item_id):
    return uuid.uuid5(_NAMESPACE, f"{location_id}#{menu_item_id}").hex


def dish_of(item):
    """The catering dish for one MENU# item, or None when it is not copied."""
    if item.get("active") is not True or item.get("alcohol") is True:
        return None
    try:
        return c.validate_dish({"name": str(item.get("name") or "")[:120],
                                "description": str(item.get("description") or "")[:500],
                                "pricePerPortion": item.get("price"), "active": True,
                                "sourceMenuItemId": item.get("menuItemId")}) | {"version": 1}
    except ValueError:
        return None


def _all(call, **request):
    while True:
        page = call(**request)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def run(ddb, location_table, menu_table, catering_menu_table, apply=False):
    counts = {"locations": 0, "menuItems": 0, "skipped": 0, "alreadyCopied": 0, "written": 0}
    locations, menu, target = (ddb.Table(n) for n in (location_table, menu_table, catering_menu_table))
    for loc in _all(locations.scan, FilterExpression=Attr("SK").begins_with("LOCATION#")
                    & Attr("cateringSettings.enabled").eq(True)):
        location_id = loc["SK"].removeprefix("LOCATION#")
        counts["locations"] += 1
        for item in _all(menu.query, KeyConditionExpression=Key("PK").eq(c.location_pk(location_id))
                         & Key("SK").begins_with("MENU#")):
            counts["menuItems"] += 1
            dish = dish_of(item)
            if dish is None:
                counts["skipped"] += 1
                continue
            key = c.menu_key(location_id, c.DISH, dish_id(location_id, item["menuItemId"]))
            if not apply:
                exists = "Item" in target.get_item(Key=key, ProjectionExpression="PK")
                counts["alreadyCopied" if exists else "written"] += 1
                continue
            try:
                target.put_item(Item={**key, **dish}, ConditionExpression="attribute_not_exists(PK)")
                counts["written"] += 1
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                counts["alreadyCopied"] += 1
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    prefix = "" if a.env == "prod" else f"{a.env}-"
    ddb = boto3.resource("dynamodb", region_name="eu-north-1")
    counts = run(ddb, f"{prefix}location", f"{prefix}menu", f"{prefix}catering-menu", apply=a.apply)
    print(("" if a.apply else "DRY RUN (nothing written; 'written' = would write): ") + str(counts))


if __name__ == "__main__":
    main()
