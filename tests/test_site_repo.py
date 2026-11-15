"""functions/site-repo: the restaurant's website repo site-<slug>. GitHub is a
fake (in-memory repos, variables, files); DynamoDB, IAM and Secrets Manager
are moto. Covers: prod creates a real customer's repo and dev sets up its
side, a retry changes nothing, the deploy role (path, boundary, tag, trust
per branch, own folder only), the isolation check, archive per environment,
the site-* name guard, and dev console test customers (their own org and
App, two sandbox roles, both branches, offboard and delete)."""

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
TENANT = "t-roma"
OTHER = "t-other"
PLAIN = "t-plain"
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
            self.orgs[target]._repo(data["name"], private=data["private"])
            self.orgs[target].calls.append((method, path))
            return 201, json.dumps({"name": data["name"]}).encode()
        if org not in self.orgs or auth != f"ghs_{org}":
            return 404, json.dumps({"message": "Not Found"}).encode()
        return self.orgs[org](method, url, headers, body)


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
        "STRIPE_PUBLISHABLE_KEY": "pk_test_x", "TURNSTILE_SITE_KEY": "0x4AAA", "GITHUB_ORG_ID": str(ORG_ID),
        **({} if environment == "prod" else {
            "TEST_GITHUB_ORG": TEST_ORG, "TEST_GITHUB_ORG_ID": str(TEST_ORG_ID), "SITE_TEMPLATE_OWNER": ORG,
            "TEST_SITES_BUCKET": f"dev-restaurant-sites-test-{account}", "TEST_DEPLOY_ROLE_PATH": "/booqy-test-sites/",
            "TEST_DEPLOY_ROLE_PREFIX": "test-site-",
            "TEST_DEPLOY_BOUNDARY_ARN": "arn:aws:iam::123456789012:policy/booqy-test-sites/dev-booqy-test-sites-deploy-boundary",
            "TEST_MAIN_DOMAIN": "test.booqy.se", "TEST_DEV_DOMAIN": "test-dev.booqy.se"}),
        **({"DEV_LINK_ROLE_ARN": "arn:aws:iam::877653167825:role/booqy-sites-prod-link",
            "DEV_PLATFORM_TENANTS_FUNCTION_ARN": "arn:aws:lambda:eu-north-1:877653167825:function:dev-platform-tenants:live"}
           if environment == "prod" else {}),
    }


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
        # TENANT and OTHER: dev tenants prod linked (their repos are born in
        # prod); PLAIN: a prod customer; TEST: a dev console test customer.
        for tid, slug, kind in ((TENANT, "test-roma", "prod-link"), (OTHER, "test-other", "prod-link"),
                                (PLAIN, "roma", None), (TEST, "pasta", "test")):
            table.put_item(Item={"PK": f"TENANT#{tid}", "SK": "PROFILE", "slug": slug, "name": "Roma",
                                 "status": "active", "primaryDomain": f"{slug}.dev.booqy.se",
                                 **({"siteKind": kind} if kind else {})})
        table.update_item(Key={"PK": f"TENANT#{TEST}", "SK": "PROFILE"}, UpdateExpression="SET primaryDomain = :d, #n = :n",
                          ExpressionAttributeNames={"#n": "name"},
                          ExpressionAttributeValues={":d": "pasta.test.booqy.se", ":n": "Pasta"})
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sm = boto3.client("secretsmanager", region_name=REGION)
        sm.create_secret(Name="booqy/prod/github-app-sites", SecretString=json.dumps({"app_id": "123", "private_key": key}))
        # dev: the Customer Sites key, and the Test Sites key nested under "test" (same secret, no IAM change)
        sm.create_secret(Name="booqy/dev/github-app-sites", SecretString=json.dumps(
            {"app_id": "123", "private_key": key, "test": {"app_id": TEST_APP_ID, "private_key": key}}))
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

        def use(environment):
            for k in _env_for("dev"):
                monkeypatch.delenv(k, raising=False)
            for k, v in _env_for(environment).items():
                monkeypatch.setenv(k, v)
            fn = load_fn("site-repo", f"site_repo_{environment}")
            fn._tokens.clear()
            real = fn.GitHub
            monkeypatch.setattr(fn, "GitHub", lambda token, transport=None: real(token, world_gh))
            # moto has no policy simulator: answer like IAM does for the
            # role's own policy (boundary with the tenantId tag).
            monkeypatch.setattr(fn, "_check_isolation", _simulated_isolation(fn))
            monkeypatch.setattr(fn, "_dev_link", lambda task: links.append(task) or {"tenantId": "t-dev-roma"})
            return fn

        links = []
        yield {"use": use, "github": fake, "test_github": world_gh.orgs[TEST_ORG], "world": world_gh,
               "table": table, "iam": iam, "links": links}


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


def test_a_private_site_repo_is_made_public_when_provisioned(world):
    # Site repos are public (owner 2026-11-15); an older private one turns public on the next provision.
    fn = world["use"]("dev")
    gh = world["github"]
    gh._repo("site-test-roma", private=True, default_branch="dev")
    out = fn.handler({"action": "provision", "tenantId": TENANT}, None)
    assert out["status"] == "ready"
    assert gh.repos["site-test-roma"]["private"] is False
    assert ("PATCH", f"/repos/{ORG}/site-test-roma") in gh.calls


def _prod_born(world, name="site-test-roma"):
    """The repo prod created (dev never creates one in BooqyCustomers)."""
    world["github"]._repo(name, default_branch="dev", private=False)


def test_dev_sets_up_its_side_of_a_prod_born_repo_and_deploys(world):
    fn = world["use"]("dev")
    _prod_born(world)
    out = fn.handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    repo = gh.repos["site-test-roma"]
    assert out == {"status": "ready", "repo": "site-test-roma", "repoUrl": f"https://github.com/{ORG}/site-test-roma"}
    assert repo["private"] is False and repo["default_branch"] == "dev" and repo["branches"] == {"dev", "main"}
    assert repo["oidc"] == {"use_default": True}
    assert repo["variables"] == {
        "DEV_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-sites/dev-site-test-roma",
        "DEV_SITE_BUCKET": "dev-restaurant-sites-123456789012", "DEV_TENANT_ID": TENANT,
        "DEV_API_BASE_URL": "https://api-dev.example"}
    doc = gh.booqy_json("site-test-roma")
    assert doc == {"slug": "test-roma", "name": "Roma", "environments": {"dev": {
        "tenantId": TENANT, "apiBaseUrl": "https://api-dev.example", "menuImageBaseUrl": "https://img.example",
        "stripePublishableKey": "pk_test_x", "turnstileSiteKey": "0x4AAA", "previewUrl": "https://test-roma.dev.booqy.se"}}}
    # The booqy.json commit on dev is the first deploy; no extra dispatch.
    assert gh.dispatches == []
    assert ("POST", f"/repos/{ORG}/site-template/generate") not in gh.calls
    site = world["table"].get_item(Key={"PK": f"TENANT#{TENANT}", "SK": "SITE"})["Item"]
    assert site["status"] == "ready" and site["repo"] == "site-test-roma" and "lastError" not in site


def test_the_deploy_role_writes_only_its_own_folder_from_its_own_branch(world):
    fn = world["use"]("dev")
    _prod_born(world)
    _prod_born(world, "site-test-other")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    role = world["iam"].get_role(RoleName="dev-site-test-roma")["Role"]
    assert role["Path"] == "/booqy-sites/"
    assert role["PermissionsBoundary"]["PermissionsBoundaryArn"].endswith(":policy/booqy-sites/dev-booqy-sites-deploy-boundary")
    assert {t["Key"]: t["Value"] for t in role["Tags"]}["tenantId"] == TENANT
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    repo_id = world["github"].repos["site-test-roma"]["id"]
    assert trust == [{"Effect": "Allow", "Principal": {"Federated": _env_for("dev")["OIDC_PROVIDER_ARN"]},
                      "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
                          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                          "token.actions.githubusercontent.com:sub":
                              f"repo:{ORG}@{ORG_ID}/site-test-roma@{repo_id}:ref:refs/heads/dev"}}}]
    policy = world["iam"].get_role_policy(RoleName="dev-site-test-roma", PolicyName="deploy-own-site")["PolicyDocument"]
    assert policy["Statement"][0]["Resource"] == f"arn:aws:s3:::dev-restaurant-sites-123456789012/sites/{TENANT}/*"
    assert policy["Statement"][1]["Condition"] == {"StringLike": {"s3:prefix": f"sites/{TENANT}/*"}}
    # Two customers: neither role names the other's folder.
    fn.handler({"action": "provision", "tenantId": OTHER}, None)
    other = world["iam"].get_role_policy(RoleName="dev-site-test-other", PolicyName="deploy-own-site")["PolicyDocument"]
    assert TENANT not in json.dumps(other) and OTHER not in json.dumps(policy)


def test_isolation_check_refuses_a_role_that_could_write_elsewhere(world, monkeypatch):
    for k, v in _env_for("dev").items():
        monkeypatch.setenv(k, v)
    fn = load_fn("site-repo", "site_repo_iso")
    stub = type("Iam", (), {})()
    stub.simulate_principal_policy = lambda **kw: {"EvaluationResults": [
        {"EvalResourceName": "arn:aws:s3:::${BucketName}/${KeyName}", "EvalDecision": "implicitDeny",
         "ResourceSpecificResults": [{"EvalResourceName": arn, "EvalResourceDecision": "allowed"}]}
        for arn in kw["ResourceArns"]]}
    fn._clients["iam"] = stub
    with pytest.raises(fn.SiteRepoError, match="isolation check failed"):
        fn._check_isolation("arn:aws:iam::1:role/booqy-sites/dev-site-test-roma", TENANT)


def test_prod_finds_the_dev_repo_and_adds_only_its_own_part(world):
    _prod_born(world)
    world["use"]("dev").handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    first_id = gh.repos["site-test-roma"]["id"]
    world["table"].update_item(Key={"PK": f"TENANT#{TENANT}", "SK": "PROFILE"},
                               UpdateExpression="SET primaryDomain = :d", ExpressionAttributeValues={":d": "roma.se"})
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": TENANT}, None)
    repo = gh.repos["site-test-roma"]
    assert repo["id"] == first_id and len([n for n in gh.repos if n.startswith("site-test-roma")]) == 1
    assert repo["variables"]["PROD_AWS_ROLE_ARN"] == "arn:aws:iam::123456789012:role/booqy-sites/site-test-roma"
    assert repo["variables"]["DEV_TENANT_ID"] == TENANT
    doc = gh.booqy_json("site-test-roma")
    assert set(doc["environments"]) == {"dev", "prod"}
    assert doc["environments"]["prod"]["domain"] == "roma.se"
    assert doc["environments"]["prod"]["previewUrl"] == "https://test-roma.booqy.se"
    trust = world["iam"].get_role(RoleName="site-test-roma")["Role"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"].endswith(":ref:refs/heads/main")
    # Prod deploys main once at setup: live on <slug>.booqy.se, no own domain needed.
    assert gh.dispatches == [("site-test-roma", "main")]
    repo["runs"]["main"] = {"status": "completed", "conclusion": "success", "head_sha": "cffb0be0", "html_url": "u"}
    prod.handler({"action": "provision", "tenantId": TENANT}, None)
    assert gh.dispatches == [("site-test-roma", "main")]        # live already: Run setup again doesn't redeploy


def test_a_retry_changes_nothing_and_deploys_dev_again(world):
    _prod_born(world)
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    before = json.dumps({k: v for k, v in gh.repos["site-test-roma"].items() if k != "collaborators"}, default=sorted)
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    after = json.dumps({k: v for k, v in gh.repos["site-test-roma"].items() if k != "collaborators"}, default=sorted)
    assert before == after
    assert ("POST", f"/repos/{ORG}/site-template/generate") not in gh.calls
    assert gh.dispatches == [("site-test-roma", "dev")]


def test_a_failure_is_recorded_on_the_site_row(world, monkeypatch):
    fn = world["use"]("dev")
    _prod_born(world)
    monkeypatch.setattr(fn, "_ensure_oidc_subject", lambda gh, name, org=None: (_ for _ in ()).throw(fn.SiteRepoError("boom")))
    with pytest.raises(fn.SiteRepoError):
        fn.handler({"action": "provision", "tenantId": TENANT}, None)
    site = world["table"].get_item(Key={"PK": f"TENANT#{TENANT}", "SK": "SITE"})["Item"]
    assert site["status"] == "failed" and site["lastError"] == "boom"


def test_only_site_repos(world):
    fn = world["use"]("dev")
    for slug in ("", "Roma", "../infrastructure", "a" * 70, None):
        with pytest.raises(fn.SiteRepoError, match="refused"):
            fn.repo_name(slug)
    with pytest.raises(fn.SiteRepoError, match="refused"):
        fn._repo_path("infrastructure")
    assert fn.repo_name("test-roma") == "site-test-roma"


def test_archive_keeps_the_repo_while_the_other_environment_uses_it(world):
    _prod_born(world)
    world["use"]("dev").handler({"action": "provision", "tenantId": TENANT}, None)
    world["use"]("prod").handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    out = world["use"]("dev").handler({"action": "archive", "tenantId": TENANT}, None)
    repo = gh.repos["site-test-roma"]
    assert out["repoArchived"] is False and not repo["archived"]
    assert set(repo["variables"]) == {"PROD_AWS_ROLE_ARN", "PROD_SITE_BUCKET", "PROD_TENANT_ID", "PROD_API_BASE_URL"}
    with pytest.raises(world["iam"].exceptions.NoSuchEntityException):
        world["iam"].get_role(RoleName="dev-site-test-roma")
    out = world["use"]("prod").handler({"action": "archive", "tenantId": TENANT}, None)
    assert out["repoArchived"] is True and repo["archived"] is True and repo["variables"] == {}
    assert "site-test-roma" in gh.repos                     # archived, not deleted


def test_prod_onboarding_unarchives_an_archived_repo(world):
    _prod_born(world)
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    fn.handler({"action": "archive", "tenantId": TENANT}, None)
    assert world["github"].repos["site-test-roma"]["archived"] is True
    world["use"]("prod").handler({"action": "provision", "tenantId": TENANT}, None)
    assert world["github"].repos["site-test-roma"]["archived"] is False


def test_status_invite_and_redeploy(world):
    _prod_born(world)
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    gh.repos["site-test-roma"]["runs"]["dev"] = {
        "status": "completed", "conclusion": "success", "head_sha": "abcdef1234", "run_started_at": "2026-10-27T10:00:00Z",
        "actor": {"login": "dev-person"}, "event": "push", "html_url": "https://github.com/x/runs/1"}
    out = fn.handler({"action": "status", "tenantId": TENANT}, None)
    assert out["status"] == "ready" and out["repoExists"] is True and out["archived"] is False
    assert out["deploys"]["dev"]["sha"] == "abcdef1" and out["deploys"]["prod"] is None
    assert out["config"]["tenantId"] == TENANT and "private_key" not in json.dumps(out)
    assert fn.handler({"action": "invite", "tenantId": TENANT, "username": "@anna-dev"}, None)["result"] == "invited"
    assert gh.repos["site-test-roma"]["collaborators"] == {"anna-dev"}
    with pytest.raises(fn.SiteRepoError, match="username"):
        fn.handler({"action": "invite", "tenantId": TENANT, "username": "a/b"}, None)
    assert fn.handler({"action": "redeploy", "tenantId": TENANT}, None) == {
        "repo": "site-test-roma", "branch": "dev", "result": "started"}


def test_an_offboarded_tenant_gets_no_new_repo(world):
    fn = world["use"]("dev")
    world["table"].update_item(Key={"PK": f"TENANT#{TENANT}", "SK": "PROFILE"},
                               UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":s": "offboarded"})
    with pytest.raises(fn.SiteRepoError, match="offboarded"):
        fn.handler({"action": "provision", "tenantId": TENANT}, None)
    assert "site-test-roma" not in world["github"].repos


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
    out = fn.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert out["status"] == "legacy" and "Re-create it as a test customer" in out["message"]
    assert "site-roma" not in world["github"].repos and world["test_github"].repos == {}
    with pytest.raises(world["iam"].exceptions.NoSuchEntityException):
        world["iam"].get_role(RoleName="dev-site-roma")
    status = fn.handler({"action": "status", "tenantId": PLAIN}, None)
    assert status["status"] == "legacy" and status["kind"] == "legacy"


def test_a_dev_side_prod_adopted_before_site_kinds_keeps_working(world):
    # No siteKind, not created by the link, but dev already set up its side of prod's repo.
    fn = world["use"]("dev")
    _prod_born(world, "site-roma")
    world["table"].put_item(Item={"PK": f"TENANT#{PLAIN}", "SK": "SITE", "status": "ready", "repo": "site-roma"})
    assert fn.handler({"action": "provision", "tenantId": PLAIN}, None)["status"] == "ready"
    assert world["github"].repos["site-roma"]["variables"]["DEV_TENANT_ID"] == PLAIN


def test_dev_never_creates_a_real_customers_repo(world):
    fn = world["use"]("dev")
    with pytest.raises(fn.SiteRepoError, match="born in the production console"):
        fn.handler({"action": "provision", "tenantId": TENANT}, None)
    assert "site-test-roma" not in world["github"].repos


def test_prod_creates_the_repo_and_asks_dev_for_its_side(world):
    prod = world["use"]("prod")
    out = prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert out["status"] == "ready" and "site-roma" in world["github"].repos
    assert world["links"] == [{"action": "mirror", "prodTenantId": PLAIN, "name": "Roma", "slug": "roma"}]
    site = world["table"].get_item(Key={"PK": f"TENANT#{PLAIN}", "SK": "SITE"})["Item"]
    assert site["devTenantId"] == "t-dev-roma"
    # The dev tenant's own onboarding then finds the repo and sets up dev for real.
    world["table"].put_item(Item={"PK": "TENANT#t-dev-roma", "SK": "PROFILE", "slug": "roma", "name": "Roma",
                                  "status": "active", "siteKind": "prod-link"})
    dev = world["use"]("dev")
    assert dev.handler({"action": "provision", "tenantId": "t-dev-roma"}, None)["status"] == "ready"
    variables = world["github"].repos["site-roma"]["variables"]
    assert variables["DEV_TENANT_ID"] == "t-dev-roma" and variables["PROD_TENANT_ID"] == PLAIN
    assert set(world["github"].booqy_json("site-roma")["environments"]) == {"dev", "prod"}


def test_prod_archive_removes_the_dev_side_too(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    prod.handler({"action": "archive", "tenantId": PLAIN}, None)
    assert world["links"][-1] == {"action": "archive", "slug": "roma"}


def test_an_older_custom_oidc_subject_is_reset_to_the_default(world):
    fn = world["use"]("dev")
    world["github"]._repo("site-test-roma", default_branch="dev",
                          oidc={"use_default": False, "include_claim_keys": ["repository_owner_id", "repository_id", "ref"]})
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    assert world["github"].repos["site-test-roma"]["oidc"] == {"use_default": True}


def test_prod_asks_dev_about_its_domain_through_the_link(world):
    prod = world["use"]("prod")
    prod.handler({"action": "devDomain", "tenantId": PLAIN, "op": "mount", "host": "dev.roma.se"}, None)
    assert world["links"][-1] == {"action": "domain", "op": "mount", "slug": "roma", "host": "dev.roma.se"}
    with pytest.raises(prod.SiteRepoError):
        prod.handler({"action": "devDomain", "tenantId": PLAIN, "op": "delete"}, None)
    dev = world["use"]("dev")
    with pytest.raises(dev.SiteRepoError):
        dev.handler({"action": "devDomain", "tenantId": TENANT, "op": "status"}, None)


def test_prod_delete_archives_both_sides_then_deletes_the_repo(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    out = prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    assert out == {"status": "deleted", "repo": "site-roma", "repoDeleted": True}
    assert "site-roma" not in world["github"].repos
    assert world["links"][-1] == {"action": "archive", "slug": "roma"}
    site = world["table"].get_item(Key={"PK": f"TENANT#{PLAIN}", "SK": "SITE"})["Item"]
    assert site["status"] == "deleted" and "roleArn" not in site
    # Run again (a purge that stopped halfway): nothing left to do.
    assert prod.handler({"action": "delete", "tenantId": PLAIN}, None)["status"] == "gone"


def test_delete_keeps_a_repo_another_environment_still_uses(world):
    _prod_born(world)
    world["use"]("dev").handler({"action": "provision", "tenantId": TENANT}, None)
    world["use"]("prod").handler({"action": "provision", "tenantId": TENANT}, None)
    out = world["use"]("dev").handler({"action": "delete", "tenantId": TENANT}, None)
    assert out["status"] == "kept" and "site-test-roma" in world["github"].repos
    assert not any(v.startswith("DEV_") for v in world["github"].repos["site-test-roma"]["variables"])


def test_dev_never_deletes_a_repo_prod_set_up(world):
    world["github"]._repo("site-roma", default_branch="dev")
    world["table"].put_item(Item={"PK": "TENANT#t-dev-roma", "SK": "PROFILE", "slug": "roma", "name": "Roma",
                                  "status": "offboarded"})
    out = world["use"]("dev").handler({"action": "delete", "tenantId": "t-dev-roma"}, None)
    assert out["status"] == "kept" and "site-roma" in world["github"].repos


def test_dev_deletes_an_older_site_test_repo_it_made_alone(world):
    _prod_born(world)
    dev = world["use"]("dev")
    dev.handler({"action": "provision", "tenantId": TENANT}, None)
    assert dev.handler({"action": "delete", "tenantId": TENANT}, None)["repoDeleted"] is True
    assert "site-test-roma" not in world["github"].repos


def test_delete_without_admin_permission_fails_with_a_hint_and_keeps_the_repo_archived(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    world["github"].refuse_delete = True
    with pytest.raises(prod.SiteRepoError, match="Administration: Read and write"):
        prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    assert world["github"].repos["site-roma"]["archived"] is True


def test_a_new_customer_with_a_deleted_slug_gets_a_fresh_repo(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    old_id = world["github"].repos["site-roma"]["id"]
    prod.handler({"action": "delete", "tenantId": PLAIN}, None)
    world["table"].put_item(Item={"PK": "TENANT#t-roma-2", "SK": "PROFILE", "slug": "roma", "name": "Roma",
                                  "status": "active"})
    assert prod.handler({"action": "provision", "tenantId": "t-roma-2"}, None)["status"] == "ready"
    repo = world["github"].repos["site-roma"]
    assert repo["id"] != old_id and repo["variables"]["PROD_TENANT_ID"] == "t-roma-2"


def test_a_repo_owned_by_another_org_gets_no_deploy_role(world):
    # GitHub follows a transferred repo's old path: the answer can name another owner.
    world["github"]._repo("site-test-roma", default_branch="dev", owner={"id": 1, "login": "SomeoneElse"})
    fn = world["use"]("dev")
    with pytest.raises(fn.SiteRepoError, match="owner is not BooqyCustomers"):
        fn.handler({"action": "provision", "tenantId": TENANT}, None)
    assert world["iam"].list_roles(PathPrefix="/booqy-sites/")["Roles"] == []
    assert world["github"].repos["site-test-roma"]["variables"] == {}
    site = world["table"].get_item(Key={"PK": f"TENANT#{TENANT}", "SK": "SITE"})["Item"]
    assert site["status"] == "failed" and "owner is not" in site["lastError"]


def test_the_owner_check_ignores_case_and_the_subject_keeps_githubs_casing(world):
    world["github"]._repo("site-test-roma", default_branch="dev", owner={"id": ORG_ID, "login": "booqycustomers"})
    fn = world["use"]("dev")
    assert fn.handler({"action": "provision", "tenantId": TENANT}, None)["status"] == "ready"
    trust = world["iam"].get_role(RoleName="dev-site-test-roma")["Role"]["AssumeRolePolicyDocument"]
    sub = trust["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert sub.startswith(f"repo:booqycustomers@{ORG_ID}/site-test-roma@")


def test_generate_and_delete_use_the_site_org(world):
    prod = world["use"]("prod")
    prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    gh = world["github"]
    assert ("POST", f"/repos/{ORG}/site-template/generate") in gh.calls
    assert ("GET", f"/orgs/{ORG}/installation") in world["world"].calls
    assert ("PATCH", f"/repos/{ORG}/site-roma") in gh.calls
    assert prod.handler({"action": "delete", "tenantId": PLAIN}, None)["repoDeleted"] is True
    assert ("DELETE", f"/repos/{ORG}/site-roma") in gh.calls
    assert "site-roma" not in gh.repos
    assert all(not p.startswith("/repos/") or p.startswith(f"/repos/{ORG}/") for _, p in gh.calls)


# ------------------------------------------------- test customers (dev console)
TEST_BUCKET = "dev-restaurant-sites-test-123456789012"


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
    # Nothing in the real customers' org.
    assert set(world["github"].repos) == {"site-template"}
    assert not any(p.startswith(f"/repos/{ORG}/") and not p.endswith("/generate") for _, p in world["world"].calls)
    assert repo["variables"] == {
        "DEV_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-test-sites/test-site-pasta-dev",
        "DEV_SITE_BUCKET": TEST_BUCKET, "DEV_TENANT_ID": TEST, "DEV_API_BASE_URL": "https://api-dev.example",
        "DEV_SITE_PREFIX": "dev/sites",
        "SANDBOX_MAIN_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/booqy-test-sites/test-site-pasta-main",
        "SANDBOX_MAIN_SITE_BUCKET": TEST_BUCKET, "SANDBOX_MAIN_TENANT_ID": TEST,
        "SANDBOX_MAIN_API_BASE_URL": "https://api-dev.example", "SANDBOX_MAIN_SITE_PREFIX": "main/sites"}
    common = {"tenantId": TEST, "apiBaseUrl": "https://api-dev.example", "menuImageBaseUrl": "https://img.example",
              "stripePublishableKey": "pk_test_x", "turnstileSiteKey": "0x4AAA"}
    expected = {"slug": "pasta", "name": "Pasta", "environments": {
        "dev": {**common, "previewUrl": "https://pasta.test-dev.booqy.se"},
        "test": {**common, "previewUrl": "https://pasta.test.booqy.se"}}}
    # On both branches, so main's first build has its section; each commit deploys its branch.
    assert tg.booqy_json("site-pasta", "dev") == expected and tg.booqy_json("site-pasta", "main") == expected
    assert tg.dispatches == []
    site = world["table"].get_item(Key={"PK": f"TENANT#{TEST}", "SK": "SITE"})["Item"]
    assert site["status"] == "ready" and site["kind"] == "test" and site["org"] == TEST_ORG
    assert site["roleArns"] == {"dev": repo["variables"]["DEV_AWS_ROLE_ARN"],
                                "main": repo["variables"]["SANDBOX_MAIN_AWS_ROLE_ARN"]}


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
    # No role among the real customers' site roles.
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
    # A main role that could write dev's folder fails.
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


def test_offboarding_a_test_customer_removes_both_roles_and_all_variables_and_archives(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    out = fn.handler({"action": "archive", "tenantId": TEST}, None)
    repo = world["test_github"].repos["site-pasta"]
    assert out["repoArchived"] is True and repo["archived"] is True and repo["variables"] == {}
    assert world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"] == []
    site = world["table"].get_item(Key={"PK": f"TENANT#{TEST}", "SK": "SITE"})["Item"]
    assert site["status"] == "archived" and "roleArns" not in site
    # Restore: Set up again brings it back.
    assert fn.handler({"action": "provision", "tenantId": TEST}, None)["status"] == "ready"
    assert repo["archived"] is False and len(world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"]) == 2


def test_deleting_a_test_customer_deletes_its_repo_in_the_test_org(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TEST}, None)
    out = fn.handler({"action": "delete", "tenantId": TEST}, None)
    assert out == {"status": "deleted", "repo": "site-pasta", "repoDeleted": True}
    assert "site-pasta" not in world["test_github"].repos
    assert ("DELETE", f"/repos/{TEST_ORG}/site-pasta") in world["world"].calls
    assert world["iam"].list_roles(PathPrefix="/booqy-test-sites/")["Roles"] == []
    assert fn.handler({"action": "delete", "tenantId": TEST}, None)["status"] == "gone"


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


def test_the_test_org_can_never_be_the_real_one(world, monkeypatch):
    fn = world["use"]("dev")
    monkeypatch.setenv("TEST_GITHUB_ORG", ORG.lower())
    with pytest.raises(fn.SiteRepoError, match="real customers' org"):
        fn.handler({"action": "provision", "tenantId": TEST}, None)
