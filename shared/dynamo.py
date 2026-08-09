"""Thin boto3 DynamoDB helpers.

Avoids every function re-creating its own boto3 resource/client at import
time - table()/client() lazily create one shared object per cold start and
reuse it for every call a function makes.
"""

import boto3

_resource = None
_client = None


def table(name: str):
    """High-level resource Table - use for GetItem/PutItem/Query/Scan/
    UpdateItem, where boto3's friendlier Python-type marshalling is enough.
    """
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource.Table(name)


def client():
    """Low-level client - use for anything the resource API doesn't cover,
    e.g. transact_write_items(). Note this uses DynamoDB's raw typed
    attribute format ({"S": "..."}, {"BOOL": True}, ...), not plain Python
    values like the resource API accepts.
    """
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client
