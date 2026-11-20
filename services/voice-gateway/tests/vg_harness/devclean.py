"""Deletes what a live replay against the real dev restaurant (VG_WORLD=dev) wrote in dev. Owner: builder 'harness'.

  python -m vg_harness.devclean snapshot /path/before.json      before the run
  python -m vg_harness.devclean clean /path/before.json [--dry]  after it (also lowers the USAGE row)

Only rows the replay made go: its own CALL rows (call reference "connect-gw-harness-..."), their turn rows and
CALLBACK rows, the reservations a phone call of the replay caller made after the first of those calls (PENDING,
RESERVATION, RID and LINK rows), and the dev-slot-occupancy SLOT and LOCK rows of their tables. Rows other sessions
wrote meanwhile (other call references) are listed and left alone. Profile booqy-dev (`VG_AWS_PROFILE`).
"""

from __future__ import annotations

import json
import os
import sys

REGION = "eu-north-1"
TENANT = "TENANT#01m4fqaga9r6s2e1pcx7t0wx3z"
LOCATION = "01m4fqaga9ca7cfvybw0ptzezr"
CALLER = "+46700000000"
TABLES = ("dev-phone-agent", "dev-reservation", "dev-slot-occupancy")


def _resource():
    import boto3
    return boto3.Session(profile_name=os.environ.get("VG_AWS_PROFILE", "booqy-dev"), region_name=REGION).resource("dynamodb")


def _scan(res, table: str) -> set[tuple[str, str]]:
    tb, out, kw = res.Table(table), set(), {"ProjectionExpression": "PK,SK"}
    while True:
        page = tb.scan(**kw)
        out |= {(i["PK"], i["SK"]) for i in page["Items"]}
        if "LastEvaluatedKey" not in page:
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def snapshot(path: str) -> None:
    res = _resource()
    json.dump({t: sorted(_scan(res, t)) for t in TABLES}, open(path, "w"))
    print("snapshot", {t: len(v) for t, v in json.load(open(path)).items()})


def clean(path: str, dry: bool = False) -> None:
    res = _resource()
    before = {t: {tuple(x) for x in rows} for t, rows in json.load(open(path)).items()}
    new = {t: _scan(res, t) - before[t] for t in TABLES}
    pa, reservations = res.Table("dev-phone-agent"), res.Table("dev-reservation")
    mine, others = {}, []
    for pk, sk in sorted(new["dev-phone-agent"]):
        if sk.startswith("CALL#") and "#T#" not in sk:
            item = pa.get_item(Key={"PK": pk, "SK": sk}).get("Item", {})
            if "gw-harness" in item.get("callRef", ""):
                mine[item["callId"]] = item
            else:
                others.append(item.get("callRef"))
    print("calls of the replay", len(mine), "| rows of other sessions left alone, calls:", others)
    dels, billed = [], 0
    for cid, item in mine.items():
        billed += int(item.get("billedMinutes", 0))
        day = item["SK"].split("#")[1]
        dels += [("dev-phone-agent", pk, sk) for pk, sk in new["dev-phone-agent"]
                 if sk.startswith(f"CALL#{day}#{cid}") or sk.startswith(f"CALLBACK#{cid}")]
    since = min((i.get("startedAt") or i.get("answeredAt") or "9" for i in mine.values()), default="9")
    rids, tables = set(), set()
    for pk, sk in sorted(new["dev-reservation"]):
        if sk.startswith("RESERVATION#"):
            item = reservations.get_item(Key={"PK": pk, "SK": sk}).get("Item") or {}
            if item.get("source") == "phone" and item.get("customerPhone") == CALLER and item.get("createdAt", "") >= since:
                rids.add(item["reservationId"])
                tables |= {(item["date"], item["startTime"], item["endTime"], t) for t in item.get("tableIds", [])}
    for pk, sk in new["dev-reservation"]:
        rid = sk.split("#")[-1]
        if pk.startswith("LINK#"):
            if (reservations.get_item(Key={"PK": pk, "SK": sk}).get("Item") or {}).get("reservationId") in rids:
                dels.append(("dev-reservation", pk, sk))
        elif rid in rids:
            dels.append(("dev-reservation", pk, sk))
    for pk, sk in new["dev-slot-occupancy"]:
        if any(sk in (f"SLOT#{d}#{s}-{e}#{t}", f"LOCK#{d}#{t}") for d, s, e, t in tables):
            dels.append(("dev-slot-occupancy", pk, sk))
    print("delete", len(dels), "rows; USAGE minus", billed, "minutes and", len(mine), "calls")
    if dry:
        return
    for table, pk, sk in dels:
        res.Table(table).delete_item(Key={"PK": pk, "SK": sk})
    if mine:
        pa.update_item(Key={"PK": TENANT, "SK": sorted(pa.query(
            KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
            ExpressionAttributeValues={":p": TENANT, ":s": "USAGE#"})["Items"], key=lambda i: i["SK"])[-1]["SK"]},
            UpdateExpression="ADD #m :m, #c :c, loc.#l :m", ExpressionAttributeNames={"#m": "minutes", "#c": "calls", "#l": LOCATION},
            ExpressionAttributeValues={":m": -billed, ":c": -len(mine)})
    print("done")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "snapshot":
        snapshot(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "clean":
        clean(sys.argv[2], "--dry" in sys.argv)
    else:
        sys.exit(__doc__)
