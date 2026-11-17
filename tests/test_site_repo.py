"""functions/site-repo: the restaurant's website repo site-<slug>. GitHub is a
fake (in-memory repos, variables, files, one App per org); DynamoDB, IAM and
Secrets Manager are moto, the CloudFront key value store a fake. Covers:
prod creates a real customer's repo and writes everything on GitHub, dev
only does the AWS side through the link (never a GitHub call, it holds only
the Test Sites key), a retry changes nothing, the deploy roles (path,
boundary, tags, trust per branch, own folder only), the isolation check,
archive and delete per environment, the site-* name guard, and dev console
test customers (their own org and App, two sandbox roles, both branches,
the sandbox host map, offboard and delete)."""

import base64
import json

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto import mock_aws

from tenant_support import load_fn

REGION = "eu-north-1"
PLAIN = "t-plain"
LEGACY = "t-legacy"
ORG = "BooqyCustomers"
ORG_ID = 356944191
TEST_ORG = "BooqyCustomersDev"
TEST_ORG_ID = 356983507
TEST_APP_ID = "5770798"
TEST = "t-test-pasta"


class FakeGitHub:
    """Just enough of the GitHub REST API for site-repo, one organisation."""

    def __init__(self, org=ORG, org_id=ORG_ID):
        self.org, self.org_id = org, org_id
        self.repos = {}
        self.calls = []
        self.dispatches = []
        self.next_id = 1000
        self.refuse_delete = False

    def _repo(self, name, **extra):
        self.next_id += 1
        repo = {"id": self.next_id, "name": name, "owner": {"id": self.org_id, "login": self.org},
                "html_url": f"https://github.com/{self.org}/{name}", "archived": False, "default_branch": "main",
                "delete_branch_on_merge": False, "branches": {"dev", "main"}, "variables": {}, "files": {},
                "collaborators": set(), "oidc": {"use_default": True}, "runs": {}}
        repo.update(extra)
        self.repos[name] = repo
        return repo

    @staticmethod
    def _public(repo):
        out = {k: repo[k] for k in ("id", "name", "owner", "html_url", "archived", "default_branch",
                                    "delete_branch_on_merge")}
        out["private"] = repo.get("private", False)
        return out

    def __call__(self, method, url, headers, body):
        path = url.removeprefix("https://api.github.com")
        data = json.loads(body) if body else None
        self.calls.append((method, path))
        out = self.handle(method, path.split("?")[0], path, data)
        status, payload = out
        return status, json.dumps(payload).encode() if payload is not None else b""

    def handle(self, method, path, full, data):
        parts = path.split("/")
        if parts[1] != "repos" or parts[2] != self.org:
            return 404, {"message": "Not Found"}       # only repos in the site org exist
        name = parts[3]
        repo = self.repos.get(name)
        rest = "/".join(parts[4:])
        if repo is None:
            return 404, {"message": "Not Found"}
        if rest == "":
            if method == "DELETE":
                if self.refuse_delete:
                    return 403, {"message": "Must have admin rights to Repository."}
                del self.repos[name]
                return 204, None
            if method == "PATCH":
                repo.update(data)
                if "visibility" in data:
                    repo["private"] = data["visibility"] != "public"
            return 200, self._public(repo)
        if rest == "git/ref/heads/dev":
            return 200, {"object": {"sha": "devsha"}}
        if rest == "git/refs" and method == "POST":
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in repo["branches"]:
                return 422, {"message": "Reference already exists"}
            repo["branches"].add(branch)
            repo.setdefault("refs", {})[branch] = data["sha"]
            return 201, {}
        if rest.startswith("branches/"):
            return (200, {}) if rest.split("/")[1] in repo["branches"] else (404, {"message": "Branch not found"})
        if rest == "actions/oidc/customization/sub":
            if method == "PUT":
                repo["oidc"] = data
                return 201, {}
            return 200, repo["oidc"]
        if rest == "actions/variables":
            if method == "POST":
                repo["variables"][data["name"]] = data["value"]
                return 201, {}
            return 200, {"variables": [{"name": k, "value": v} for k, v in repo["variables"].items()]}
        if rest.startswith("actions/variables/"):
            key = rest.split("/")[2]
            if method == "DELETE":
                repo["variables"].pop(key)
                return 204, None
            repo["variables"][key] = data["value"]
            return 204, None
        if rest == "contents/booqy.json":
            branch = data["branch"] if method == "PUT" else (full.split("ref=")[1].split("&")[0] if "ref=" in full else "dev")
            key = "booqy.json" if branch == "dev" else f"booqy.json@{branch}"
            f = repo["files"].get(key)
            if method == "GET":
                return (200, f) if f else (404, {"message": "Not Found"})
            if f and data.get("sha") != f["sha"]:
                return 409, {"message": "sha mismatch"}
            repo["files"][key] = {"content": data["content"], "sha": f"sha{len(self.calls)}"}
            return 200, {}
        if rest == "actions/workflows/deploy.yml/dispatches":
            self.dispatches.append((name, data["ref"]))
            return 204, None
        if rest.startswith("actions/workflows/deploy.yml/runs"):
            branch = full.split("branch=")[1].split("&")[0]
            run = repo["runs"].get(branch)
            return 200, {"workflow_runs": [run] if run else []}
        if rest.startswith("collaborators/"):
            user = rest.split("/")[1]
            created = user not in repo["collaborators"]
            repo["collaborators"].add(user)
            return (201, {}) if created else (204, None)
        return 404, {"message": f"unhandled {method} {path}"}

    def booqy_json(self, name, branch="dev"):
        key = "booqy.json" if branch == "dev" else f"booqy.json@{branch}"
        return json.loads(base64.b64decode(self.repos[name]["files"][key]["content"]))



class FakeGitHubWorld:
    """Both organisations behind one transport. Each org's App has its own
    installation and token; a token only works on its own org's repos, and
    the test App only finds an installation in BooqyCustomersDev."""

    APPS = {ORG: ("123", 42), TEST_ORG: (TEST_APP_ID, 43)}

    def __init__(self):
        self.orgs = {ORG: FakeGitHub(ORG, ORG_ID), TEST_ORG: FakeGitHub(TEST_ORG, TEST_ORG_ID)}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.removeprefix("https://api.github.com")
        data = json.loads(body) if body else None
        self.calls.append((method, path))
        auth = headers["Authorization"].removeprefix("Bearer ")
        for org, (app_id, installation) in self.APPS.items():
            if path == f"/orgs/{org}/installation":
                iss = jwt.decode(auth, options={"verify_signature": False})["iss"]
                return (200, json.dumps({"id": installation}).encode()) if iss == app_id else \
                    (404, json.dumps({"message": "Not Found"}).encode())
            if path == f"/app/installations/{installation}/access_tokens":
                return 201, json.dumps({"token": f"ghs_{org}"}).encode()
        parts = path.split("?")[0].split("/")
        org = parts[2] if len(parts) > 2 else ""
        if path == f"/repos/{ORG}/site-template/generate":
            target = data["owner"]
            if auth != f"ghs_{target}":
                return 403, json.dumps({"message": "Resource not accessible by integration"}).encode()
            self.orgs[target]._repo(data["name"], private=data["private"],
                                    branches={"dev", "main"} if data["include_all_branches"] else {"dev"})
            self.orgs[target].generated = data
            self.orgs[target].calls.append((method, path))
            return 201, json.dumps({"name": data["name"]}).encode()
        if org not in self.orgs or auth != f"ghs_{org}":
            return 404, json.dumps({"message": "Not Found"}).encode()
        return self.orgs[org](method, url, headers, body)



class FakeKvs:
    """The sandbox's CloudFront key value store (label -> tenantId), with
    the ETag every write must name."""

    def __init__(self):
        self.keys = {}
        self.etag = 1
        self.calls = []

    def describe_key_value_store(self, KvsARN):
        assert KvsARN == KVS_ARN
        return {"ETag": str(self.etag)}

    def _check(self, KvsARN, IfMatch):
        from botocore.exceptions import ClientError
        assert KvsARN == KVS_ARN
        if IfMatch != str(self.etag):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "etag"}}, "PutKey")
        self.etag += 1

    def put_key(self, KvsARN, Key, Value, IfMatch):
        self._check(KvsARN, IfMatch)
        self.calls.append(("put", Key, Value))
        self.keys[Key] = Value
        return {"ETag": str(self.etag)}

    def delete_key(self, KvsARN, Key, IfMatch):
        from botocore.exceptions import ClientError
        if Key not in self.keys:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no key"}}, "DeleteKey")
        self._check(KvsARN, IfMatch)
        self.calls.append(("delete", Key))
        del self.keys[Key]
        return {"ETag": str(self.etag)}


KVS_ARN = "arn:aws:cloudfront::123456789012:key-value-store/0d1c2b3a-test"
TEST_BUCKET = "dev-restaurant-sites-test-123456789012"
DEV_ROLE = "arn:aws:iam::123456789012:role/booqy-sites/dev-site-roma"


def _env_for(environment):
    prefix = "" if environment == "prod" else f"{environment}-"
    account = "343695380960" if environment == "prod" else "123456789012"
    return {
        "ENVIRONMENT": environment, "TENANT_TABLE_NAME": "test-tenant", "GITHUB_ORG": ORG,
        "GITHUB_APP_SECRET_ID": f"booqy/{environment}/github-app-sites", "SITE_TEMPLATE_REPO": "site-template",
        "SITES_BUCKET": f"{prefix}restaurant-sites-{account}", "DEPLOY_ROLE_PATH": "/booqy-sites/",
        "DEPLOY_ROLE_PREFIX": f"{prefix}site-",
        "DEPLOY_BOUNDARY_ARN": f"arn:aws:iam::123456789012:policy/booqy-sites/{prefix}booqy-sites-deploy-boundary",
        "OIDC_PROVIDER_ARN": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com",
        "API_BASE_URL": f"https://api-{environment}.example/", "MENU_IMAGE_BASE_URL": "https://img.example",
        "PREVIEW_DOMAIN": "booqy.se" if environment == "prod" else "dev.booqy.se",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_x", "GITHUB_ORG_ID": str(ORG_ID),
        **({} if environment == "prod" else {
            "TEST_GITHUB_ORG": TEST_ORG, "TEST_GITHUB_ORG_ID": str(TEST_ORG_ID), "SITE_TEMPLATE_OWNER": ORG,
            "TEST_SITES_BUCKET": f"dev-restaurant-sites-test-{account}", "TEST_DEPLOY_ROLE_PATH": "/booqy-test-sites/",
            "TEST_DEPLOY_ROLE_PREFIX": "test-site-",
            "TEST_DEPLOY_BOUNDARY_ARN": "arn:aws:iam::123456789012:policy/booqy-test-sites/dev-booqy-test-sites-deploy-boundary",
            "TEST_MAIN_DOMAIN": "test.booqy.se", "TEST_DEV_DOMAIN": "test-dev.booqy.se",
            "TEST_SITES_KVS_ARN": KVS_ARN}),
        **({"DEV_LINK_ROLE_ARN": "arn:aws:iam::877653167825:role/booqy-sites-prod-link",
            "DEV_PLATFORM_TENANTS_FUNCTION_ARN": "arn:aws:lambda:eu-north-1:877653167825:function:dev-platform-tenants:live"}
           if environment == "prod" else {}),
    }


def _profile(table, tid, slug, kind=None, **extra):
    table.put_item(Item={"PK": f"TENANT#{tid}", "SK": "PROFILE", "slug": slug, "name": slug.title(),
                         "status": "active", **({"siteKind": kind} if kind else {}), **extra})


def _claim(table, label, tid):
    table.put_item(Item={"PK": f"DOMAIN#{label}.test.booqy.se", "SK": "TENANT", "tenantId": tid})


@pytest.fixture
def world(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
                       "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName="test-tenant", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        # One table for both accounts (ids never collide). PLAIN: a prod
        # customer; TEST: a dev console test customer (its slug host claimed
        # by onboarding); LEGACY: a dev customer from before test sites.
        _profile(table, PLAIN, "roma", primaryDomain="roma.booqy.se")
        _profile(table, TEST, "pasta", "test", primaryDomain="pasta.test.booqy.se")
        _profile(table, LEGACY, "gamla", primaryDomain="gamla.dev.booqy.se")
        _claim(table, "pasta", TEST)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sm = boto3.client("secretsmanager", region_name=REGION)
        sm.create_secret(Name="booqy/prod/github-app-sites", SecretString=json.dumps({"app_id": "123", "private_key": key}))
        # dev holds ONLY the Test Sites key (owner 2026-11-15: key isolation).
        sm.create_secret(Name="booqy/dev/github-app-sites", SecretString=json.dumps(
            {"app_id": TEST_APP_ID, "private_key": key}))
        iam = boto3.client("iam", region_name=REGION)
        for prefix in ("dev-", ""):
            iam.create_policy(PolicyName=f"{prefix}booqy-sites-deploy-boundary", Path="/booqy-sites/",
                              PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                                  {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
        iam.create_policy(PolicyName="dev-booqy-test-sites-deploy-boundary", Path="/booqy-test-sites/",
                          PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                              {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
        world_gh = FakeGitHubWorld()
        fake = world_gh.orgs[ORG]
        fake._repo("site-template")
        kvs = FakeKvs()
        links, dev_calls = [], []
        mods = {}

        def set_env(environment):
            for k in _env_for("dev"):
                monkeypatch.delenv(k, raising=False)
            for k, v in _env_for(environment).items():
                monkeypatch.setenv(k, v)

        def as_dev(call):
            """Run call in dev, as dev platform-tenants would through the
            link; records any GitHub call dev made (there must be none)."""
            before = len(world_gh.calls)
            set_env("dev")
            try:
                return call(mods["dev"])
            finally:
                dev_calls.extend(world_gh.calls[before:])
                set_env("prod")

        def dev_link(task):
            """booqy-console site.site_task in dev, in short: mirror creates
            or adopts t-dev-<slug> and asks dev site-repo for the dev side."""
            links.append(task)
            slug = task["slug"]
            dev_tid = f"t-dev-{slug}"
            if task["action"] == "archive":
                return as_dev(lambda fn: {"tenantId": dev_tid, **fn.handler({"action": "archive", "tenantId": dev_tid},
                                                                              None)})
            assert task["action"] == "mirror"
            item = table.get_item(Key={"PK": f"TENANT#{dev_tid}", "SK": "PROFILE"}).get("Item")
            if not item:
                _profile(table, dev_tid, slug, "prod-link", primaryDomain=f"{slug}.dev.booqy.se")
            site = as_dev(lambda fn: fn.handler({"action": "linkProvision", "tenantId": dev_tid,
                                                 "repo": task["repo"]}, None))
            return {"tenantId": dev_tid, "status": "created" if not item else "adopted", "site": site}

        def load(environment):
            fn = load_fn("site-repo", f"site_repo_{environment}")
            fn._tokens.clear()
            real = fn.GitHub
            monkeypatch.setattr(fn, "GitHub", lambda token, transport=None: real(token, world_gh))
            # moto has no policy simulator: answer like IAM does for the
            # role's own policy (boundary with the tenantId tag).
            monkeypatch.setattr(fn, "_check_isolation", _simulated_isolation(fn))
            monkeypatch.setattr(fn, "_dev_link", dev_link)
            fn._clients["cloudfront-keyvaluestore"] = kvs
            return fn

        def use(environment):
            set_env(environment)
            if environment not in mods:
                mods[environment] = load(environment)
            if "dev" not in mods:
                mods["dev"] = load("dev")
            return mods[environment]

        yield {"use": use, "github": fake, "test_github": world_gh.orgs[TEST_ORG], "world": world_gh,
               "table": table, "iam": iam, "links": links, "dev_calls": dev_calls, "kvs": kvs, "as_dev": as_dev}


def _simulated_isolation(fn):
    real = fn._check_isolation

    def check(role_arn, tenant_id, *args):
        iam = fn.client("iam")
        name = role_arn.split("/")[-1]
        policy = iam.get_role_policy(RoleName=name, PolicyName="deploy-own-site")["PolicyDocument"]
        allowed = [s["Resource"] for s in policy["Statement"] if s["Sid"] == "OwnSiteFolder"]

        def simulate(**kwargs):
            # Like IAM: the result is named by a pattern, not the ARN asked about.
            results = []
            for arn in kwargs["ResourceArns"]:
                ok = any(arn.startswith(a.rstrip("*")) for a in allowed)
                results.append({"EvalResourceName": "arn:aws:s3:::${BucketName}/${KeyName}",
                                "EvalDecision": "allowed" if ok else "implicitDeny"})
            return {"EvaluationResults": results}

        original = iam.simulate_principal_policy if hasattr(iam, "simulate_principal_policy") else None
        iam.simulate_principal_policy = simulate
        try:
            real(role_arn, tenant_id, *args)
        finally:
            if original:
                iam.simulate_principal_policy = original
    return check


def _site(world, tid):
    return world["table"].get_item(Key={"PK": f"TENANT#{tid}", "SK": "SITE"}).get("Item") or {}


def _real_org_calls(world):
    return [c for c in world["world"].calls if c[1].startswith(f"/repos/{ORG}/") or c[1] == f"/orgs/{ORG}/installation"]


# ------------------------------------------------------- real customers (prod)
def test_prod_creates_the_repo_and_writes_both_sides_while_dev_only_does_aws(world):
    prod = world["use"]("prod")
    out = prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    gh = world["github"]
    repo = gh.repos["site-roma"]
    assert out == {"status": "ready", "repo": "site-roma", "repoUrl": f"https://github.com/{ORG}/site-roma"}
    assert repo["private"] is False and repo["default_branch"] == "dev" and repo["oidc"] == {"use_default": True}
    # Prod asks dev for its AWS side, naming the repo (dev can't read BooqyCustomers).
    assert world["links"] == [{"action": "mirror", "prodTenantId": PLAIN, "name": "Roma", "slug": "roma", "repo": {
        "name": "site-roma", "id": repo["id"], "html_url": f"https://github.com/{ORG}/site-roma",
        "owner": {"login": ORG, "id": ORG_ID}}}]
    # Dev made NO GitHub call: it holds only the Test Sites key.
    assert world["dev_calls"] == []
    # Prod wrote PROD_* and dev's DEV_* with its own key.
    assert repo["variables"] == {
        "PROD_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-sites/site-roma",
        "PROD_SITE_BUCKET": "restaurant-sites-343695380960", "PROD_TENANT_ID": PLAIN,
        "PROD_API_BASE_URL": "https://api-prod.example",
        "DEV_AWS_ROLE_ARN": DEV_ROLE, "DEV_SITE_BUCKET": "dev-restaurant-sites-123456789012",
        "DEV_TENANT_ID": "t-dev-roma", "DEV_API_BASE_URL": "https://api-dev.example"}
    common = {"menuImageBaseUrl": "https://img.example", "stripePublishableKey": "pk_test_x"}
    assert gh.booqy_json("site-roma") == {"slug": "roma", "name": "Roma", "environments": {
        "prod": {"tenantId": PLAIN, "apiBaseUrl": "https://api-prod.example", **common,
                 "previewUrl": "https://roma.booqy.se"},
        "dev": {"tenantId": "t-dev-roma", "apiBaseUrl": "https://api-dev.example", **common,
                "previewUrl": "https://roma.dev.booqy.se"}}}
    # One booqy.json commit on dev deploys dev; main is dispatched once.
    assert gh.dispatches == [("site-roma", "main")]
    assert _site(world, PLAIN)["devTenantId"] == "t-dev-roma"
    dev_site = _site(world, "t-dev-roma")
    assert dev_site["status"] == "ready" and dev_site["roleArn"] == DEV_ROLE and dev_site["managedBy"] == "prod"


def test_the_dev_role_trusts_only_the_dev_branch_of_the_repo_prod_named(world):
    world["use"]("prod").handler({"action": "provision", "tenantId": PLAIN}, None)
    repo_id = world["github"].repos["site-roma"]["id"]
    for name, branch, path in (("site-roma", "main", "sites"), ("dev-site-roma", "dev", "sites")):
        role = world["iam"].get_role(RoleName=name)["Role"]
        assert role["Path"] == "/booqy-sites/"
        assert {t["Key"]: t["Value"] for t in role["Tags"]}["tenantId"] == (PLAIN if branch == "main" else "t-dev-roma")
        sub = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
        assert sub == {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                       "token.actions.githubusercontent.com:sub":
                           f"repo:{ORG}@{ORG_ID}/site-roma@{repo_id}:ref:refs/heads/{branch}"}
    dev_policy = world["iam"].get_role_policy(RoleName="dev-site-roma", PolicyName="deploy-own-site")["PolicyDocument"]
    assert dev_policy["Statement"][0]["Resource"] == "arn:aws:s3:::dev-restaurant-sites-123456789012/sites/t-dev-roma/*"
    assert world["iam"].get_role(RoleName="dev-site-roma")["Role"]["PermissionsBoundary"][
        "PermissionsBoundaryArn"].endswith(":policy/booqy-sites/dev-booqy-sites-deploy-boundary")


@pytest.mark.parametrize("repo, error", [
    ({"name": "site-roma", "id": 7, "owner": {"login": "SomeoneElse", "id": ORG_ID}}, "owner is not BooqyCustomers"),
    ({"name": "site-roma", "id": 7, "owner": {"login": ORG, "id": TEST_ORG_ID}}, f"owner id is not {ORG_ID}"),
    ({"name": "site-roma", "id": 7, "owner": {"login": TEST_ORG, "id": TEST_ORG_ID}}, "owner is not BooqyCustomers"),
    ({"name": "site-other", "id": 7, "owner": {"login": ORG, "id": ORG_ID}}, "prod must name site-roma"),
    ({"name": "site-roma", "id": "7", "owner": {"login": ORG, "id": ORG_ID}}, "prod must name site-roma"),
    (None, "prod must name site-roma"),
])
def test_dev_writes_a_real_customers_trust_only_for_the_booqycustomers_repo_prod_names(world, repo, error):
    fn = world["use"]("dev")
    _profile(world["table"], "t-dev-roma", "roma", "prod-link")
    with pytest.raises(fn.SiteRepoError, match=error):
        fn.handler({"action": "linkProvision", "tenantId": "t-dev-roma", "repo": repo}, None)
    assert world["iam"].list_roles(PathPrefix="/booqy-sites/")["Roles"] == []
    assert world["world"].calls == []


def test_link_provision_is_only_for_prod_linked_dev_tenants_and_needs_the_org_id(world, monkeypatch):
    fn = world["use"]("dev")
    repo = {"name": "site-pasta", "id": 7, "owner": {"login": ORG, "id": ORG_ID}}
    with pytest.raises(fn.SiteRepoError, match="not linked from prod"):
        fn.handler({"action": "linkProvision", "tenantId": TEST, "repo": repo}, None)
    _profile(world["table"], "t-dev-roma", "roma", "prod-link")
    monkeypatch.setenv("GITHUB_ORG_ID", "")
    with pytest.raises(fn.SiteRepoError, match="GITHUB_ORG_ID is not set"):
        fn.handler({"action": "linkProvision", "tenantId": "t-dev-roma",
                    "repo": {**repo, "name": "site-roma"}}, None)
    prod = world["use"]("prod")
    with pytest.raises(prod.SiteRepoError, match="refused"):
        prod.handler({"action": "linkProvision", "tenantId": PLAIN, "repo": repo}, None)


def test_prod_takes_only_dev_variables_from_devs_answer(world):
    prod = world["use"]("prod")
    good = {"site": {"variables": {"DEV_AWS_ROLE_ARN": DEV_ROLE, "DEV_SITE_BUCKET": "b", "DEV_TENANT_ID": "t",
                                   "DEV_API_BASE_URL": "u", "PROD_AWS_ROLE_ARN": "arn:aws:iam::1:role/x"},
                     "config": {"tenantId": "t"}}}
    variables, config = prod._dev_side(good)
    assert set(variables) == set(prod.DEV_VARIABLES) and config == {"tenantId": "t"}
    for bad in ({}, {"site": {"variables": {**good["site"]["variables"], "DEV_AWS_ROLE_ARN": "nope"},
                              "config": {}}}):
        with pytest.raises(prod.SiteRepoError, match="no deploy role"):
            prod._dev_side(bad)


def test_dev_never_calls_github_for_a_real_customers_repo(world):
    fn = world["use"]("dev")
    # Its client is only for the test org, and its key only opens that org.
    with pytest.raises(fn.SiteRepoError, match=f"no GitHub calls for {ORG}"):
        fn.github(mode=fn.Mode({"siteKind": "prod-link"}))
    _profile(world["table"], "t-dev-roma", "roma", "prod-link")
    out = fn.handler({"action": "provision", "tenantId": "t-dev-roma"}, None)    # its own onboarding
    assert out["status"] == "waiting" and out["managedBy"] == "prod"
    status = fn.handler({"action": "status", "tenantId": "t-dev-roma"}, None)
    assert status["managedBy"] == "prod" and "deploys" not in status
    for action in ({"action": "invite", "username": "anna"}, {"action": "redeploy"}):
        with pytest.raises(fn.SiteRepoError, match="managed from the production console"):
            fn.handler({**action, "tenantId": "t-dev-roma"}, None)
    assert fn.handler({"action": "archive", "tenantId": "t-dev-roma"}, None)["managedBy"] == "prod"
    assert fn.handler({"action": "delete", "tenantId": "t-dev-roma"}, None) == {
        "status": "kept", "repo": "site-roma", "repoDeleted": False, "reason": "prod owns this repo"}
    assert world["world"].calls == []


def test_a_retry_changes_nothing_and_deploys_dev_again(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    gh = world["github"]
    repo = gh.repos["site-roma"]
    repo["runs"]["main"] = {"status": "completed", "conclusion": "success", "head_sha": "cffb0be0", "html_url": "u"}
    before = json.dumps({k: v for k, v in repo.items() if k != "collaborators"}, default=sorted)
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    after = json.dumps({k: v for k, v in repo.items() if k != "collaborators"}, default=sorted)
    assert before == after
    assert [n for n in gh.repos if n.startswith("site-roma")] == ["site-roma"]
    # main is live: not again; dev: booqy.json unchanged, so dispatched.
    assert gh.dispatches == [("site-roma", "main"), ("site-roma", "dev")]
    assert world["dev_calls"] == []


def test_an_existing_repo_with_dev_and_prod_variables_keeps_working(world):
    # BooqyCustomers/site-test-release: set up before key isolation, with
    # DEV_* (dev role dev-site-test-release) and PROD_*.
    table = world["table"]
    _profile(table, "t-release", "test-release")
    _profile(table, "t-dev-test-release", "test-release", None, createdBy="prod-site-repo")
    gh = world["github"]
    gh._repo("site-test-release", default_branch="dev")
    repo = gh.repos["site-test-release"]
    repo["variables"] = {
        "PROD_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-sites/site-test-release",
        "PROD_SITE_BUCKET": "restaurant-sites-343695380960", "PROD_TENANT_ID": "t-release",
        "PROD_API_BASE_URL": "https://api-prod.example",
        "DEV_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-sites/dev-site-test-release",
        "DEV_SITE_BUCKET": "dev-restaurant-sites-123456789012", "DEV_TENANT_ID": "t-dev-test-release",
        "DEV_API_BASE_URL": "https://api-dev.example"}
    before = dict(repo["variables"])
    world["use"]("prod").handler({"action": "provision", "tenantId": "t-release"}, None)
    assert repo["variables"] == before
    sub = world["iam"].get_role(RoleName="dev-site-test-release")["Role"]["AssumeRolePolicyDocument"][
        "Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert sub == f"repo:{ORG}@{ORG_ID}/site-test-release@{repo['id']}:ref:refs/heads/dev"
    assert world["dev_calls"] == []


def test_prod_archive_removes_both_sides_and_archives_the_repo(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    out = prod.handler({"action": "archive", "tenantId": PLAIN}, None)
    repo = world["github"].repos["site-roma"]
    assert world["links"][-1] == {"action": "archive", "slug": "roma"}
    assert out["repoArchived"] is True and repo["archived"] is True and repo["variables"] == {}
    assert world["iam"].list_roles(PathPrefix="/booqy-sites/")["Roles"] == []
    assert _site(world, "t-dev-roma")["status"] == "archived" and world["dev_calls"] == []
    # Onboarding again (Restore) unarchives it.
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert repo["archived"] is False and repo["variables"]["DEV_TENANT_ID"] == "t-dev-roma"


def test_prod_delete_archives_both_sides_then_deletes_the_repo(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    out = prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    assert out == {"status": "deleted", "repo": "site-roma", "repoDeleted": True}
    assert "site-roma" not in world["github"].repos
    assert world["links"][-1] == {"action": "archive", "slug": "roma"}
    site = _site(world, PLAIN)
    assert site["status"] == "deleted" and "roleArn" not in site
    # Run again (a purge that stopped halfway): nothing left to do.
    assert prod.handler({"action": "delete", "tenantId": PLAIN}, None)["status"] == "gone"


def test_a_private_site_repo_is_made_public_when_provisioned(world):
    world["github"]._repo("site-roma", private=True, default_branch="dev")
    assert world["use"]("prod").handler({"action": "provision", "tenantId": PLAIN}, None)["status"] == "ready"
    assert world["github"].repos["site-roma"]["private"] is False


def test_isolation_check_refuses_a_role_that_could_write_elsewhere(world, monkeypatch):
    world["use"]("dev")
    fn = load_fn("site-repo", "site_repo_iso")
    stub = type("Iam", (), {})()
    stub.simulate_principal_policy = lambda **kw: {"EvaluationResults": [
        {"EvalResourceName": "arn:aws:s3:::${BucketName}/${KeyName}", "EvalDecision": "implicitDeny",
         "ResourceSpecificResults": [{"EvalResourceName": arn, "EvalResourceDecision": "allowed"}]}
        for arn in kw["ResourceArns"]]}
    fn._clients["iam"] = stub
    with pytest.raises(fn.SiteRepoError, match="isolation check failed"):
        fn._check_isolation("arn:aws:iam::1:role/booqy-sites/dev-site-roma", "t-dev-roma")


def test_a_failure_is_recorded_on_the_site_row(world, monkeypatch):
    prod = world["use"]("prod")
    monkeypatch.setattr(prod, "_ensure_oidc_subject", lambda gh, name, org=None: (_ for _ in ()).throw(prod.SiteRepoError("boom")))
    with pytest.raises(prod.SiteRepoError):
        prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    site = _site(world, PLAIN)
    assert site["status"] == "failed" and site["lastError"] == "boom"


def test_only_site_repos(world):
    fn = world["use"]("dev")
    for slug in ("", "Roma", "../infrastructure", "a" * 70, None):
        with pytest.raises(fn.SiteRepoError, match="refused"):
            fn.repo_name(slug)
    with pytest.raises(fn.SiteRepoError, match="refused"):
        fn._repo_path("infrastructure")
    assert fn.repo_name("test-roma") == "site-test-roma"


def test_status_invite_and_redeploy_in_prod(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    gh = world["github"]
    gh.repos["site-roma"]["runs"]["dev"] = {
        "status": "completed", "conclusion": "success", "head_sha": "abcdef1234", "run_started_at": "2026-10-27T10:00:00Z",
        "actor": {"login": "dev-person"}, "event": "push", "html_url": "https://github.com/x/runs/1"}
    out = prod.handler({"action": "status", "tenantId": PLAIN}, None)
    assert out["status"] == "ready" and out["repoExists"] is True and out["archived"] is False
    assert out["deploys"]["dev"]["sha"] == "abcdef1" and out["deploys"]["prod"] is None
    assert out["config"]["tenantId"] == PLAIN and "private_key" not in json.dumps(out)
    assert prod.handler({"action": "invite", "tenantId": PLAIN, "username": "@anna-dev"}, None)["result"] == "invited"
    assert gh.repos["site-roma"]["collaborators"] == {"anna-dev"}
    with pytest.raises(prod.SiteRepoError, match="username"):
        prod.handler({"action": "invite", "tenantId": PLAIN, "username": "a/b"}, None)
    assert prod.handler({"action": "redeploy", "tenantId": PLAIN}, None) == {
        "repo": "site-roma", "branch": "main", "result": "started"}


def test_an_offboarded_tenant_gets_no_new_repo(world):
    prod = world["use"]("prod")
    world["table"].update_item(Key={"PK": f"TENANT#{PLAIN}", "SK": "PROFILE"},
                               UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":s": "offboarded"})
    with pytest.raises(prod.SiteRepoError, match="offboarded"):
        prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert "site-roma" not in world["github"].repos


def test_the_app_jwt_is_rs256_and_short_lived(world):
    fn = world["use"]("dev")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    token = fn._app_jwt("123", pem, 1_000_000)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}


def test_a_dev_tenant_from_before_test_sites_is_legacy_and_gets_nothing(world):
    fn = world["use"]("dev")
    out = fn.handler({"action": "provision", "tenantId": LEGACY}, None)
    assert out["status"] == "legacy" and "Re-create it as a test customer" in out["message"]
    assert world["test_github"].repos == {} and world["world"].calls == []
    status = fn.handler({"action": "status", "tenantId": LEGACY}, None)
    assert status["status"] == "legacy" and status["kind"] == "legacy"


def test_an_older_custom_oidc_subject_is_reset_to_the_default(world):
    world["github"]._repo("site-roma", default_branch="dev",
                          oidc={"use_default": False, "include_claim_keys": ["repository_owner_id", "repository_id", "ref"]})
    world["use"]("prod").handler({"action": "provision", "tenantId": PLAIN}, None)
    assert world["github"].repos["site-roma"]["oidc"] == {"use_default": True}


def test_prod_asks_dev_about_its_domain_through_the_link(world, monkeypatch):
    prod = world["use"]("prod")
    seen = []
    monkeypatch.setattr(prod, "_dev_link", lambda task: seen.append(task) or {"state": "live"})
    prod.handler({"action": "devDomain", "tenantId": PLAIN, "op": "mount", "host": "dev.roma.se"}, None)
    assert seen == [{"action": "domain", "op": "mount", "slug": "roma", "host": "dev.roma.se"}]
    with pytest.raises(prod.SiteRepoError):
        prod.handler({"action": "devDomain", "tenantId": PLAIN, "op": "delete"}, None)
    dev = world["use"]("dev")
    with pytest.raises(dev.SiteRepoError):
        dev.handler({"action": "devDomain", "tenantId": TEST, "op": "status"}, None)


def test_delete_without_admin_permission_fails_with_a_hint_and_keeps_the_repo_archived(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    world["github"].refuse_delete = True
    with pytest.raises(prod.SiteRepoError, match="Booqy Customer Sites App needs .*Administration: Read and write"):
        prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    assert world["github"].repos["site-roma"]["archived"] is True


def test_a_new_customer_with_a_deleted_slug_gets_a_fresh_repo(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    old_id = world["github"].repos["site-roma"]["id"]
    prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    _profile(world["table"], "t-roma-2", "roma")
    assert prod.handler({"action": "provision", "tenantId": "t-roma-2"}, None)["status"] == "ready"
    repo = world["github"].repos["site-roma"]
    assert repo["id"] != old_id and repo["variables"]["PROD_TENANT_ID"] == "t-roma-2"
    sub = world["iam"].get_role(RoleName="dev-site-roma")["Role"]["AssumeRolePolicyDocument"][
        "Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert sub.endswith(f"/site-roma@{repo['id']}:ref:refs/heads/dev")      # the new repo, not the old one


def test_a_repo_owned_by_another_org_gets_no_deploy_role(world):
    # GitHub follows a transferred repo's old path: the answer can name another owner.
    world["github"]._repo("site-roma", default_branch="dev", owner={"id": 1, "login": "SomeoneElse"})
    prod = world["use"]("prod")
    with pytest.raises(prod.SiteRepoError, match="owner is not BooqyCustomers"):
        prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert world["iam"].list_roles(PathPrefix="/booqy-sites/")["Roles"] == []
    assert world["github"].repos["site-roma"]["variables"] == {} and world["links"] == []
    site = _site(world, PLAIN)
    assert site["status"] == "failed" and "owner is not" in site["lastError"]


def test_the_owner_check_ignores_case_and_the_subject_keeps_githubs_casing(world):
    world["github"]._repo("site-roma", default_branch="dev", owner={"id": ORG_ID, "login": "booqycustomers"})
    assert world["use"]("prod").handler({"action": "provision", "tenantId": PLAIN}, None)["status"] == "ready"
    for name in ("site-roma", "dev-site-roma"):
        trust = world["iam"].get_role(RoleName=name)["Role"]["AssumeRolePolicyDocument"]
        sub = trust["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        assert sub.startswith(f"repo:booqycustomers@{ORG_ID}/site-roma@")


def test_generate_and_delete_use_the_site_org_with_prods_app(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    gh = world["github"]
    assert gh.generated["include_all_branches"] is True and "refs" not in gh.repos["site-roma"]
    assert ("POST", f"/repos/{ORG}/site-template/generate") in gh.calls
    assert ("GET", f"/orgs/{ORG}/installation") in world["world"].calls
    assert prod.handler({"action": "delete", "tenantId": PLAIN}, None)["repoDeleted"] is True
    assert ("DELETE", f"/repos/{ORG}/site-roma") in gh.calls
    assert all(not p.startswith("/repos/") or p.startswith(f"/repos/{ORG}/") for _, p in gh.calls)


# ------------------------------------------------- test customers (dev console)
def test_a_test_customer_gets_a_public_repo_in_the_test_org_from_the_shared_template(world):
    fn = world["use"]("dev")
    out = fn.handler({"action": "provision", "tenantId": TEST}, None)
    tg = world["test_github"]
    assert out == {"status": "ready", "repo": "site-pasta", "repoUrl": f"https://github.com/{TEST_ORG}/site-pasta"}
    repo = tg.repos["site-pasta"]
    assert repo["private"] is False and repo["default_branch"] == "dev" and repo["owner"]["id"] == TEST_ORG_ID
    # Generated from the public BooqyCustomers/site-template, with the Test Sites App's own token.
    assert ("POST", f"/repos/{ORG}/site-template/generate") in tg.calls
    assert ("GET", f"/orgs/{TEST_ORG}/installation") in world["world"].calls
    # Only the template's dev branch: main is made from it (the template's
    # main has no sandbox deploy), never copied from the template's main.
    assert tg.generated["include_all_branches"] is False and repo["refs"] == {"main": "devsha"}
    # Nothing else in the real customers' org.
    assert set(world["github"].repos) == {"site-template"}
    assert [c for c in _real_org_calls(world) if not c[1].endswith("/generate")] == []
    assert repo["variables"] == {
        "DEV_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-test-sites/test-site-pasta-dev",
        "DEV_SITE_BUCKET": TEST_BUCKET, "DEV_TENANT_ID": TEST, "DEV_API_BASE_URL": "https://api-dev.example",
        "DEV_SITE_PREFIX": "dev/sites",
        "SANDBOX_MAIN_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-test-sites/test-site-pasta-main",
        "SANDBOX_MAIN_SITE_BUCKET": TEST_BUCKET, "SANDBOX_MAIN_TENANT_ID": TEST,
        "SANDBOX_MAIN_API_BASE_URL": "https://api-dev.example", "SANDBOX_MAIN_SITE_PREFIX": "main/sites"}
    common = {"tenantId": TEST, "apiBaseUrl": "https://api-dev.example", "menuImageBaseUrl": "https://img.example",
              "stripePublishableKey": "pk_test_x"}
    expected = {"slug": "pasta", "name": "Pasta", "environments": {
        "dev": {**common, "previewUrl": "https://pasta.test-dev.booqy.se"},
        "test": {**common, "previewUrl": "https://pasta.test.booqy.se"}}}
    # On both branches, so main's first build has its section; each commit deploys its branch.
    assert tg.booqy_json("site-pasta", "dev") == expected and tg.booqy_json("site-pasta", "main") == expected
    assert tg.dispatches == []
    site = _site(world, TEST)
    assert site["status"] == "ready" and site["kind"] == "test" and site["org"] == TEST_ORG
    assert site["roleArns"] == {"dev": repo["variables"]["DEV_AWS_ROLE_ARN"],
                                "main": repo["variables"]["SANDBOX_MAIN_AWS_ROLE_ARN"]}
    # The sandbox's CloudFront Function finds both hosts by the slug label.
    assert world["kvs"].keys == {"pasta": TEST} and site["testHosts"] == {"pasta"}


def test_a_test_customer_has_one_role_per_branch_each_for_its_own_stage_folder(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    repo_id = world["test_github"].repos["site-pasta"]["id"]
    for stage in ("main", "dev"):
        role = world["iam"].get_role(RoleName=f"test-site-pasta-{stage}")["Role"]
        assert role["Path"] == "/booqy-test-sites/"
        assert role["PermissionsBoundary"]["PermissionsBoundaryArn"].endswith(
            ":policy/booqy-test-sites/dev-booqy-test-sites-deploy-boundary")
        assert {t["Key"]: t["Value"] for t in role["Tags"]} == {"tenantId": TEST, "stage": stage, "repo": "site-pasta"}
        sub = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
        assert sub == {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                       "token.actions.githubusercontent.com:sub":
                           f"repo:{TEST_ORG}@{TEST_ORG_ID}/site-pasta@{repo_id}:ref:refs/heads/{stage}"}
        policy = world["iam"].get_role_policy(RoleName=f"test-site-pasta-{stage}", PolicyName="deploy-own-site")["PolicyDocument"]
        assert policy["Statement"][0]["Resource"] == f"arn:aws:s3:::{TEST_BUCKET}/{stage}/sites/{TEST}/*"
        assert policy["Statement"][1]["Condition"] == {"StringLike": {"s3:prefix": f"{stage}/sites/{TEST}/*"}}
    assert world["iam"].list_roles(PathPrefix="/booqy-sites/")["Roles"] == []


def test_a_test_roles_isolation_check_covers_the_other_stage_the_sandbox_files_and_the_real_bucket(world, monkeypatch):
    world["use"]("dev")
    fn = load_fn("site-repo", "site_repo_iso_test")          # the real check, not the fixture's simulation
    mode = fn.Mode({"siteKind": "test"})
    main = mode.stage("main")
    probes = fn._isolation_probes(TEST, mode, main)
    assert probes == {
        f"arn:aws:s3:::{TEST_BUCKET}/main/sites/{TEST}/index.html": "allowed",
        f"arn:aws:s3:::{TEST_BUCKET}/main/sites/isolation-probe-{TEST}/index.html": "implicitDeny",
        f"arn:aws:s3:::{TEST_BUCKET}/index.html": "implicitDeny",
        f"arn:aws:s3:::{TEST_BUCKET}/dev/sites/{TEST}/index.html": "implicitDeny",
        f"arn:aws:s3:::{TEST_BUCKET}/_platform/robots.txt": "implicitDeny",
        f"arn:aws:s3:::dev-restaurant-sites-123456789012/sites/{TEST}/index.html": "implicitDeny"}
    seen = []
    stub = type("Iam", (), {})()

    def simulate(**kw):
        seen.append(kw["ContextEntries"])
        arn = kw["ResourceArns"][0]
        return {"EvaluationResults": [{"EvalDecision": "allowed" if f"/main/sites/{TEST}/" in arn else "implicitDeny"}]}
    stub.simulate_principal_policy = simulate
    monkeypatch.setitem(fn._clients, "iam", stub)
    fn._check_isolation("arn:aws:iam::1:role/booqy-test-sites/test-site-pasta-main", TEST, mode, main)
    assert {"ContextKeyName": "aws:PrincipalTag/stage", "ContextKeyValues": ["main"], "ContextKeyType": "string"} in seen[0]
    stub.simulate_principal_policy = lambda **kw: {"EvaluationResults": [{"EvalDecision": "allowed"}]}
    with pytest.raises(fn.SiteRepoError, match="isolation check failed"):
        fn._check_isolation("arn:aws:iam::1:role/booqy-test-sites/test-site-pasta-main", TEST, mode, main)


def test_a_test_repo_owned_by_another_org_or_id_gets_no_role(world):
    tg = world["test_github"]
    tg._repo("site-pasta", default_branch="dev", private=False, owner={"id": ORG_ID, "login": TEST_ORG})
    fn = world["use"]("dev")
    with pytest.raises(fn.SiteRepoError, match=f"owner id is not {TEST_ORG_ID}"):
        fn.handler({"action": "provision", "tenantId": TEST}, None)
    assert world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"] == []
    # And a real customer's repo never gets a role from the test org's settings.
    with pytest.raises(fn.SiteRepoError, match="owner is not BooqyCustomers"):
        fn.trust_policy({"name": "site-x", "id": 1, "owner": {"login": TEST_ORG, "id": TEST_ORG_ID}},
                        fn.Mode({"siteKind": "prod-link"}), fn.Mode({}).stages[0])


def test_a_retry_of_a_test_customer_changes_nothing_and_deploys_both_branches(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    tg = world["test_github"]
    before = json.dumps({k: v for k, v in tg.repos["site-pasta"].items() if k != "collaborators"}, default=sorted)
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    after = json.dumps({k: v for k, v in tg.repos["site-pasta"].items() if k != "collaborators"}, default=sorted)
    assert before == after
    assert tg.dispatches == [("site-pasta", "dev"), ("site-pasta", "main")]
    tg.repos["site-pasta"]["runs"]["main"] = {"status": "completed", "conclusion": "success", "head_sha": "abc1234",
                                               "html_url": "u"}
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    assert tg.dispatches[-1] == ("site-pasta", "dev")           # main is live: not deployed again


def test_a_mapped_own_domain_is_in_the_test_section(world):
    fn = world["use"]("dev")
    world["table"].update_item(Key={"PK": f"TENANT#{TEST}", "SK": "PROFILE"}, UpdateExpression="SET primaryDomain = :d",
                               ExpressionAttributeValues={":d": "example-se.test.booqy.se"})
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    doc = world["test_github"].booqy_json("site-pasta", "main")
    assert doc["environments"]["test"]["domain"] == "example-se.test.booqy.se"
    assert "domain" not in doc["environments"]["dev"]


def test_status_redeploy_and_invite_for_a_test_customer(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    tg = world["test_github"]
    tg.repos["site-pasta"]["runs"]["main"] = {"status": "completed", "conclusion": "success", "head_sha": "abcdef12",
                                               "html_url": "u"}
    out = fn.handler({"action": "status", "tenantId": TEST}, None)
    assert out["kind"] == "test" and out["org"] == TEST_ORG and out["repoExists"] is True
    assert set(out["deploys"]) == {"dev", "main"} and out["deploys"]["main"]["sha"] == "abcdef1"
    assert out["urls"] == {"dev": "https://pasta.test-dev.booqy.se", "main": "https://pasta.test.booqy.se"}
    assert fn.handler({"action": "redeploy", "tenantId": TEST, "branch": "main"}, None)["branch"] == "main"
    assert fn.handler({"action": "redeploy", "tenantId": TEST}, None)["branch"] == "dev"
    assert tg.dispatches == [("site-pasta", "main"), ("site-pasta", "dev")]
    with pytest.raises(fn.SiteRepoError, match="branch must be one of"):
        fn.handler({"action": "redeploy", "tenantId": TEST, "branch": "prod"}, None)
    assert fn.handler({"action": "invite", "tenantId": TEST, "username": "anna-dev"}, None)["result"] == "invited"
    assert tg.repos["site-pasta"]["collaborators"] == {"anna-dev"}


def test_a_mapped_domain_label_goes_into_the_host_map_only_for_its_claim_holder(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    kvs = world["kvs"]
    with pytest.raises(fn.SiteRepoError, match="not claimed by"):
        fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": "example-se"}, None)
    _claim(world["table"], "example-se", TEST)
    assert fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": "example-se"}, None)["result"] == "done"
    assert kvs.keys == {"pasta": TEST, "example-se": TEST}
    for bad in ("Example.se", "a.b", "-x", "x" * 64):
        with pytest.raises(fn.SiteRepoError, match="not a host label"):
            fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": bad}, None)
    with pytest.raises(fn.SiteRepoError, match="slug's own host"):
        fn.handler({"action": "testHost", "tenantId": TEST, "op": "delete", "label": "pasta"}, None)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "delete", "label": "example-se"}, None)
    assert kvs.keys == {"pasta": TEST} and _site(world, TEST)["testHosts"] == {"pasta"}
    # Never another tenant's: a real customer has no sandbox hosts at all.
    _profile(world["table"], "t-dev-roma", "roma", "prod-link")
    with pytest.raises(fn.SiteRepoError, match="only a test customer"):
        fn.handler({"action": "testHost", "tenantId": "t-dev-roma", "op": "put", "label": "roma"}, None)


def test_a_live_mapped_domain_makes_the_slug_answer_with_a_301_and_back(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    kvs = world["kvs"]
    with pytest.raises(fn.SiteRepoError, match="not claimed by"):
        fn.handler({"action": "testHost", "tenantId": TEST, "op": "redirect", "label": "example-se"}, None)
    _claim(world["table"], "example-se", TEST)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": "example-se"}, None)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "redirect", "label": "example-se"}, None)
    assert kvs.keys == {"pasta": ">example-se", "example-se": TEST}
    for bad in ("pasta", "a.b", "-x"):
        with pytest.raises(fn.SiteRepoError, match="not a host label"):
            fn.handler({"action": "testHost", "tenantId": TEST, "op": "redirect", "label": bad}, None)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "redirect", "label": ""}, None)
    assert kvs.keys == {"pasta": TEST, "example-se": TEST}
    _profile(world["table"], "t-dev-roma", "roma", "prod-link")
    with pytest.raises(fn.SiteRepoError, match="only a test customer"):
        fn.handler({"action": "testHost", "tenantId": "t-dev-roma", "op": "redirect", "label": ""}, None)


def test_the_host_map_write_retries_when_another_writer_got_in_between(world, monkeypatch):
    fn = world["use"]("dev")
    kvs = world["kvs"]
    real = kvs.describe_key_value_store
    stale = iter([{"ETag": "0"}])
    monkeypatch.setattr(kvs, "describe_key_value_store", lambda KvsARN: next(stale, None) or real(KvsARN))
    fn._kvs_write(fn.Mode({"siteKind": "test"}), "put", "pasta", TEST)
    assert kvs.keys == {"pasta": TEST}
    assert fn._kvs_write(fn.Mode({"siteKind": "test"}), "delete", "nothing-here") is False


def test_offboarding_a_test_customer_removes_roles_variables_and_hosts_and_archives(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    _claim(world["table"], "example-se", TEST)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": "example-se"}, None)
    # Offboarding's domain-detach released the claims before site-repo archive runs.
    for label in ("pasta", "example-se"):
        world["table"].delete_item(Key={"PK": f"DOMAIN#{label}.test.booqy.se", "SK": "TENANT"})
    out = fn.handler({"action": "archive", "tenantId": TEST}, None)
    repo = world["test_github"].repos["site-pasta"]
    assert out["repoArchived"] is True and repo["archived"] is True and repo["variables"] == {}
    assert world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"] == []
    assert world["kvs"].keys == {}
    site = _site(world, TEST)
    assert site["status"] == "archived" and "roleArns" not in site and "testHosts" not in site
    # Restore: onboarding claims the slug host again and Set up brings it back.
    _claim(world["table"], "pasta", TEST)
    assert fn.handler({"action": "provision", "tenantId": TEST}, None)["status"] == "ready"
    assert repo["archived"] is False and len(world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"]) == 2
    assert world["kvs"].keys == {"pasta": TEST}


def test_a_label_another_tenant_claimed_meanwhile_is_left_alone(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    _claim(world["table"], "example-se", TEST)
    fn.handler({"action": "testHost", "tenantId": TEST, "op": "put", "label": "example-se"}, None)
    _claim(world["table"], "example-se", "t-someone-else")
    world["kvs"].keys["example-se"] = "t-someone-else"
    fn.handler({"action": "archive", "tenantId": TEST}, None)
    assert world["kvs"].keys == {"example-se": "t-someone-else"}


def test_deleting_a_test_customer_deletes_its_repo_and_hosts(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    out = fn.handler({"action": "delete", "tenantId": TEST}, None)
    assert out == {"status": "deleted", "repo": "site-pasta", "repoDeleted": True}
    assert "site-pasta" not in world["test_github"].repos
    assert ("DELETE", f"/repos/{TEST_ORG}/site-pasta") in world["world"].calls
    assert world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"] == []
    assert world["kvs"].keys == {}
    # A purge after the repo is gone still clears a host left in the map.
    world["kvs"].keys["pasta"] = TEST
    assert fn.handler({"action": "delete", "tenantId": TEST}, None)["status"] == "gone"
    assert world["kvs"].keys == {}


def test_a_test_customers_delete_refusal_names_the_test_app(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    world["test_github"].refuse_delete = True
    with pytest.raises(fn.SiteRepoError, match="Booqy Test Sites App"):
        fn.handler({"action": "delete", "tenantId": TEST}, None)


def test_prod_refuses_test_customers_and_test_settings(world, monkeypatch):
    prod = world["use"]("prod")
    with pytest.raises(prod.SiteRepoError, match="test customer in prod"):
        prod.handler({"action": "provision", "tenantId": TEST}, None)
    monkeypatch.setenv("TEST_GITHUB_ORG", TEST_ORG)
    with pytest.raises(prod.SiteRepoError, match="TEST_\\* settings in prod"):
        prod.handler({"action": "provision", "tenantId": PLAIN}, None)


def test_test_sites_off_in_dev_is_a_clear_error(world, monkeypatch):
    fn = world["use"]("dev")
    monkeypatch.delenv("TEST_GITHUB_ORG")
    with pytest.raises(fn.SiteRepoError, match="test sites are not switched on"):
        fn.handler({"action": "provision", "tenantId": TEST}, None)
    with pytest.raises(fn.SiteRepoError, match="dev calls GitHub only for test customers"):
        fn.github()


def test_the_test_org_can_never_be_the_real_one(world, monkeypatch):
    fn = world["use"]("dev")
    monkeypatch.setenv("TEST_GITHUB_ORG", ORG.lower())
    with pytest.raises(fn.SiteRepoError, match="real customers' org"):
        fn.handler({"action": "provision", "tenantId": TEST}, None)


def test_a_delete_racing_another_delete_ends_as_gone_not_as_an_error(world):
    # Delete customer and the hourly purge sweep both delete the repo: the
    # other one removes it while this one is half way (after its first GET).
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    tg = world["test_github"]
    handle = tg.handle

    def racing(method, path, full, data):
        if method == "GET" and path.endswith("/actions/variables"):
            tg.repos.pop("site-pasta", None)
        return handle(method, path, full, data)

    tg.handle = racing
    assert fn.handler({"action": "delete", "tenantId": TEST}, None) == \
        {"status": "gone", "repo": "site-pasta", "repoDeleted": False}


# ------------------------------------------------------------- own domains
def test_an_own_domain_going_live_writes_booqy_json_on_both_branches(world):
    """domain-provision's UpdateSite: the prod section gets the live domain
    (the primary), dev's comes from dev through the link; dev and main both
    get the file so both sites deploy with their canonical domain."""
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    world["table"].update_item(Key={"PK": f"TENANT#{PLAIN}", "SK": "PROFILE"},
                               UpdateExpression="SET primaryDomain = :d", ExpressionAttributeValues={":d": "roma.se"})
    world["table"].update_item(Key={"PK": "TENANT#t-dev-roma", "SK": "PROFILE"},
                               UpdateExpression="SET primaryDomain = :d", ExpressionAttributeValues={":d": "dev.roma.se"})
    out = prod.handler({"action": "domainLive", "tenantId": PLAIN}, None)
    assert out["result"] == "done" and out["committed"] == {"dev": True, "main": True}
    gh = world["github"]
    for branch in ("dev", "main"):
        doc = gh.booqy_json("site-roma", branch)
        assert doc["environments"]["prod"]["domain"] == "roma.se", branch
        assert doc["environments"]["dev"]["domain"] == "dev.roma.se", branch
    # nothing changed: nothing committed again
    assert prod.handler({"action": "domainLive", "tenantId": PLAIN}, None)["committed"] == {"dev": False, "main": False}


def test_domain_live_refuses_test_customers(world):
    dev = world["use"]("dev")
    with pytest.raises(dev.SiteRepoError):
        dev.handler({"action": "domainLive", "tenantId": TEST}, None)


def test_dev_domain_ops_for_the_own_domain_pipeline(world, monkeypatch):
    prod = world["use"]("prod")
    seen = []
    monkeypatch.setattr(prod, "_dev_link", lambda task: seen.append(task) or {"state": "live"})
    for op in ("setCanonical", "unmount"):
        prod.handler({"action": "devDomain", "tenantId": PLAIN, "op": op, "host": "dev.roma.se"}, None)
    assert [t["op"] for t in seen] == ["setCanonical", "unmount"]
