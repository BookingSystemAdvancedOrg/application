"""Shared test fixtures for multi-tenancy: the tenant, location and user
tables as infrastructure creates them, two tenants, and JWT events whose
claims look like the pre-token-generation trigger's.

Use inside an active moto `mock_aws()` context:

    from tenant_support import TENANT_A, LOC_A, seed_tenancy, with_tenant_claims
    seed_tenancy(monkeypatch)
    event = with_tenant_claims(event, TENANT_A)
"""

import boto3

from shared import tenant as shared_tenant

REGION = "eu-north-1"
TENANT_TABLE = "test-tenant"
LOCATION_TABLE_DEFAULT = "test-location"
USER_TABLE_DEFAULT = "test-user"
LOCATION_INDEX = "byLocationId"
USER_INDEX = "byTenant"

TENANT_A = "01aaaaaaaaaaaaaaaaaaaaaaaa"
TENANT_B = "01bbbbbbbbbbbbbbbbbbbbbbbb"
LOC_A = "loc-a-1"
LOC_A2 = "loc-a-2"
LOC_B = "loc-b-1"
OWNER_A_SUB = "owner-a-sub"
OWNER_B_SUB = "owner-b-sub"
STAFF_A_SUB = "staff-a-sub"

ALL_FEATURES = {"reservations": True, "ordering": True, "catering": True, "terminal": True}


def _pk_sk(name, extra_attrs=(), gsis=()):
    return dict(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"},
                              *[{"AttributeName": a, "AttributeType": "S"} for a in extra_attrs]],
        BillingMode="PAY_PER_REQUEST",
        **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}),
    )


def tenant_row(tenant_id, *, status="active", features=None, max_locations=3, **extra):
    return {
        "PK": f"TENANT#{tenant_id}", "SK": "PROFILE", "tenantId": tenant_id,
        "name": f"Tenant {tenant_id[2]}", "status": status,
        "entitlements": {"maxLocations": max_locations,
                         "features": dict(ALL_FEATURES if features is None else features)},
        "locationCount": 0, **extra,
    }


def location_row(tenant_id, location_id, **extra):
    return {"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}",
            "tenantId": tenant_id, "locationId": location_id,
            "name": f"Location {location_id}", **extra}


def create_tables(monkeypatch, *, location_table=LOCATION_TABLE_DEFAULT, user_table=USER_TABLE_DEFAULT,
                  existing=()):
    """Creates the tenancy tables (skips names in `existing`, e.g. a location
    table the test file already created - it then needs the index) and sets
    the env vars infrastructure sets."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    monkeypatch.setenv("TENANT_TABLE_NAME", TENANT_TABLE)
    monkeypatch.setenv("LOCATION_TABLE_NAME", location_table)
    monkeypatch.setenv("LOCATION_ID_INDEX_NAME", LOCATION_INDEX)
    monkeypatch.setenv("USER_TENANT_INDEX_NAME", USER_INDEX)
    if TENANT_TABLE not in existing:
        ddb.create_table(**_pk_sk(TENANT_TABLE))
    loc_gsi = {"IndexName": LOCATION_INDEX, "KeySchema": [{"AttributeName": "locationId", "KeyType": "HASH"}],
               "Projection": {"ProjectionType": "ALL"}}
    if location_table not in existing:
        ddb.create_table(**_pk_sk(location_table, ["locationId"], [loc_gsi]))
    else:
        ddb.update_table(TableName=location_table,
                         AttributeDefinitions=[{"AttributeName": "locationId", "AttributeType": "S"}],
                         GlobalSecondaryIndexUpdates=[{"Create": loc_gsi}])
    if user_table and user_table not in existing:
        ddb.create_table(**_pk_sk(user_table, ["tenantId"], [{
            "IndexName": USER_INDEX,
            "KeySchema": [{"AttributeName": "tenantId", "KeyType": "HASH"},
                          {"AttributeName": "PK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}]))
    shared_tenant.reset_caches()


def seed_tenancy(monkeypatch, *, location_table=LOCATION_TABLE_DEFAULT, user_table=USER_TABLE_DEFAULT,
                 existing=(), tenant_a=None, tenant_b=None, locations_a=None, locations_b=None):
    """Two active tenants: A with LOC_A + LOC_A2 and a staff user on LOC_A,
    B with LOC_B. Override rows/locations with the keyword arguments."""
    create_tables(monkeypatch, location_table=location_table, user_table=user_table, existing=existing)
    res = boto3.resource("dynamodb", region_name=REGION)
    res.Table(TENANT_TABLE).put_item(Item=tenant_a or tenant_row(TENANT_A))
    res.Table(TENANT_TABLE).put_item(Item=tenant_b or tenant_row(TENANT_B))
    for item in (locations_a if locations_a is not None else
                 [location_row(TENANT_A, LOC_A), location_row(TENANT_A, LOC_A2)]):
        res.Table(location_table).put_item(Item=item)
    for item in (locations_b if locations_b is not None else [location_row(TENANT_B, LOC_B)]):
        res.Table(location_table).put_item(Item=item)
    if user_table:
        users = res.Table(user_table)
        users.put_item(Item={"PK": f"USER#{OWNER_A_SUB}", "SK": "PROFILE", "cognitoSub": OWNER_A_SUB,
                             "tenantId": TENANT_A, "role": "owner_user", "email": "owner@a.example",
                             "name": "Owner A", "status": "active", "createdBy": "platform-onboarding",
                             "createdAt": "2026-10-01T10:00:00.000Z"})
        users.put_item(Item={"PK": f"USER#{OWNER_B_SUB}", "SK": "PROFILE", "cognitoSub": OWNER_B_SUB,
                             "tenantId": TENANT_B, "role": "owner_user", "email": "owner@b.example",
                             "name": "Owner B", "status": "active", "createdBy": "platform-onboarding",
                             "createdAt": "2026-10-01T10:00:00.000Z"})
        users.put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE", "cognitoSub": STAFF_A_SUB,
                             "tenantId": TENANT_A, "role": "staff", "locationId": LOC_A,
                             "email": "staff@a.example", "name": "Staff A", "phone": "+46701234567",
                             "status": "active", "createdBy": OWNER_A_SUB,
                             "createdAt": "2026-10-02T10:00:00.000Z"})


def claims_for(tenant_id, role="owner_user", sub=None):
    sub = sub or {"owner_user": {TENANT_A: OWNER_A_SUB, TENANT_B: OWNER_B_SUB}.get(tenant_id, "sub"),
                  "staff_user": STAFF_A_SUB}[role]
    return {"sub": sub, "tenant_id": tenant_id, "role": role, "cognito:groups": [role]}


def with_tenant_claims(event, tenant_id=TENANT_A, role="owner_user", sub=None):
    """Returns the event with tenant-pool claims (keeps other claims)."""
    rc = event.setdefault("requestContext", {})
    jwt = rc.setdefault("authorizer", {}).setdefault("jwt", {})
    claims = dict(jwt.get("claims") or {})
    claims.update(claims_for(tenant_id, role, sub))
    jwt["claims"] = claims
    return event


def tenant_claims(sub, groups, tenant_id=TENANT_A):
    """Claims as the pre-token trigger issues them: the caller's groups plus
    tenant_id and role (role derived from the groups, as the trigger does).
    Groups without a tenant role (e.g. the retired super_user) get no role."""
    claims = {"sub": sub, "cognito:groups": groups}
    if isinstance(groups, str):
        text = groups
    elif isinstance(groups, (list, tuple)):
        text = " ".join(str(g) for g in groups)
    else:
        text = ""
    role = next((r for r in ("owner_user", "staff_user") if r in (text or "")), None)
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if role is not None:
        claims["role"] = role
    return claims


def install_tenancy(monkeypatch, location_ids_a, *, location_table=LOCATION_TABLE_DEFAULT,
                    existing=(), location_extra=None, user_table=None):
    """Tenant A owns `location_ids_a`, tenant B owns LOC_B. For handler tests
    that only need the tenant check to pass (or fail) - creates the tables
    unless listed in `existing`."""
    create_tables(monkeypatch, location_table=location_table, user_table=user_table, existing=existing)
    res = boto3.resource("dynamodb", region_name=REGION)
    res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A))
    res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B))
    for location_id in location_ids_a:
        res.Table(location_table).put_item(Item=location_row(TENANT_A, location_id, **(location_extra or {})))
    res.Table(location_table).put_item(Item=location_row(TENANT_B, LOC_B))
