"""Thin boto3 DynamoDB helpers.

Avoids every function re-creating its own boto3 resource/client at import
time - table()/client() lazily create one shared object per cold start and
reuse it for every call a function makes.
"""

import threading

import boto3

_resource = None
_client = None
_local = threading.local()


def table(name: str):
    """High-level resource Table - use for GetItem/PutItem/Query/Scan/
    UpdateItem, where boto3's friendlier Python-type marshalling is enough.

    boto3 resources are not thread-safe: worker threads (a function
    fanning out with a ThreadPoolExecutor) each get their own, made from
    their own Session. The main thread keeps the one shared resource.
    Clients (client()) are thread-safe and stay shared.
    """
    global _resource
    if threading.current_thread() is threading.main_thread():
        if _resource is None:
            _resource = boto3.resource("dynamodb")
        return _resource.Table(name)
    resource = getattr(_local, "resource", None)
    if resource is None:
        resource = _local.resource = boto3.session.Session().resource("dynamodb")
    return resource.Table(name)


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
