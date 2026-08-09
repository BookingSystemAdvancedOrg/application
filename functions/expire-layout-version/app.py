"""expire-layout-version

EventBridge Scheduler (one-time), created by activate-layout-version.
Event = {"PK": "...", "SK": "..."}. Conditionally flips isCurrent=false on
that item. Full spec: docs/LAMBDA_REFERENCE.md #15.
"""

import os

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]


def handler(event, context):
    pk = event["PK"]
    sk = event["SK"]

    # TODO: conditional UpdateItem, isCurrent=false, ConditionExpression
    # requires isCurrent=true. Catch ConditionalCheckFailedException as a
    # no-op (already expired), not an error.

    raise NotImplementedError(f"expire-layout-version not implemented for {pk}/{sk}")
