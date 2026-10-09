#!/usr/bin/env python3
"""One-off: bring an environment's data in line with multi-tenancy.

    python scripts/migrate_to_tenancy.py --env dev            # dry run: report only
    python scripts/migrate_to_tenancy.py --env dev --apply    # make the changes

1. Tenant locations (PK TENANT#<t>) created before sbs-admin wrote the
   admin app's required fields get them: timezone Europe/Stockholm, CLOSED
   every day, 2 h bookings, 0 grace, phoneNumber (E.164) from phone, and
   tenantId/locationId from the row's key. Existing values are never
   overwritten. Each tenant's locationCount is then recomputed from its
   actual location rows (the plan limit relies on it).
2. Pre-tenancy locations (PK PLATFORM) are listed. They belong to no
   tenant, so no one can reach them any more. With --apply
   --delete-platform-locations they are deleted (their menus/layouts stay
   keyed by the old locationId and are unreachable).
3. User profiles without tenantId are listed (old test logins - the
   pre-token trigger already refuses them at sign-in).

Uses your current AWS credentials; check the account first:
    aws sts get-caller-identity
"""

import argparse
import re
import sys

import boto3
from boto3.dynamodb.conditions import Attr

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULTS = {
    "timezone": "Europe/Stockholm",
    "businessHours": {d: [] for d in WEEKDAYS},
    "bookingDurationHours": 2,
    "gracePeriodHours": 0,
}


def e164(phone):
    if not phone:
        return None
    d = re.sub(r"[\s()-]", "", phone)
    d = "+" + d[2:] if d.startswith("00") else f"+46{d[1:]}" if d.startswith("0") else d if d.startswith("+") else f"+46{d}"
    return d if re.fullmatch(r"\+[1-9]\d{7,14}", d) else None


def scan(table, **kw):
    while True:
        page = table.scan(**kw)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-platform-locations", action="store_true")
    a = ap.parse_args()
    prefix = "" if a.env == "prod" else f"{a.env}-"
    account = boto3.client("sts").get_caller_identity()["Account"]
    print(f"AWS account {account}, environment {a.env}, {'APPLY' if a.apply else 'DRY RUN'}\n")
    ddb = boto3.resource("dynamodb")
    locations, users = ddb.Table(f"{prefix}location"), ddb.Table(f"{prefix}user")

    print("1. Tenant locations missing admin-app fields")
    fixed = 0
    counts = {}
    for item in scan(locations, FilterExpression=Attr("PK").begins_with("TENANT#") & Attr("SK").begins_with("LOCATION#")):
        tenant_id = item["PK"].split("#", 1)[1]
        location_id = item["SK"].split("#", 1)[1]
        counts[tenant_id] = counts.get(tenant_id, 0) + 1
        missing = {k: v for k, v in DEFAULTS.items() if k not in item}
        if item.get("tenantId") != tenant_id:
            if "tenantId" in item:
                print(f"   !! {item['PK']} {location_id}: tenantId {item['tenantId']!r} disagrees with its key - fix by hand")
                continue
            missing["tenantId"] = tenant_id
        if item.get("locationId") != location_id:
            if "locationId" in item:
                print(f"   !! {item['PK']} {location_id}: locationId {item['locationId']!r} disagrees with its key - fix by hand")
                continue
            missing["locationId"] = location_id
        number = e164(item.get("phone")) if "phoneNumber" not in item else None
        if number:
            missing["phoneNumber"] = number
        if not missing:
            continue
        fixed += 1
        print(f"   {item['PK']} {item.get('name')!r} ({location_id}): add {sorted(missing)}")
        if a.apply:
            names = {f"#k{i}": k for i, k in enumerate(missing)}
            values = {f":v{i}": v for i, v in enumerate(missing.values())}
            locations.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET " + ", ".join(f"#k{i} = if_not_exists(#k{i}, :v{i})" for i in range(len(missing))),
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(PK)")
    print(f"   {fixed} location(s){' updated' if a.apply else ' would be updated'}\n")

    print("1b. locationCount per tenant (the plan limit relies on it)")
    tenants = ddb.Table(f"{prefix}tenant")
    for profile in scan(tenants, FilterExpression=Attr("PK").begins_with("TENANT#") & Attr("SK").eq("PROFILE")):
        tenant_id = profile["PK"].split("#", 1)[1]
        actual, stored = counts.get(tenant_id, 0), profile.get("locationCount")
        if stored is not None and int(stored) == actual:
            continue
        print(f"   {profile.get('name')!r} ({tenant_id}): locationCount {stored} -> {actual}")
        if a.apply:
            tenants.update_item(Key={"PK": profile["PK"], "SK": "PROFILE"},
                                UpdateExpression="SET locationCount = :c",
                                ConditionExpression="attribute_exists(PK)",
                                ExpressionAttributeValues={":c": actual})
    print()

    print("2. Pre-tenancy locations (PK PLATFORM) - reachable by nobody")
    platform = list(scan(locations, FilterExpression=Attr("PK").eq("PLATFORM")))
    for item in platform:
        print(f"   {item.get('name')!r} {item.get('address', '')!r} ({item.get('locationId')})")
        if a.apply and a.delete_platform_locations:
            locations.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    action = "deleted" if a.apply and a.delete_platform_locations else "left (add --apply --delete-platform-locations to delete)"
    print(f"   {len(platform)} row(s) {action}\n")

    print("3. User profiles without a tenant (refused at sign-in)")
    orphans = list(scan(users, FilterExpression=Attr("SK").eq("PROFILE") & Attr("tenantId").not_exists()))
    for item in orphans:
        print(f"   {item.get('email')} role={item.get('role')} sub={item.get('cognitoSub')}")
    print(f"   {len(orphans)} profile(s) - delete these logins in Cognito + the user table if they are old tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
