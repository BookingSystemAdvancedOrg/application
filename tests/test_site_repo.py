"""functions/site-repo: the restaurant's website repo site-<slug>. GitHub is a
fake (in-memory repos, variables, files); DynamoDB, IAM and Secrets Manager
are moto. Covers: dev creates the repo from the template and deploys, prod
finds the same repo and adds only its own part, a retry changes nothing, the
deploy role (path, boundary, tag, trust per branch, own folder only), the
isolation check, archive per environment, and the site-* name guard."""

import base64
import json

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto import mock_aws

from test_card_payments import load_fn

REGION = "eu-north-1"
TENANT = "t-roma"
OTHER = "t-other"
PLAIN = "t-plain"
ORG = "BookingSystemAdvancedOrg"


class FakeGitHub:
    """Just enough of the GitHub REST API for site-repo."""

    def __init__(self):
        self.repos = {}
        self.calls = []
        self.dispatches = []
        self.next_id = 1000

    def _repo(self, name, **extra):
        self.next_id += 1
        repo = {"id": self.next_id, "name": name, "owner": {"id": 307760488, "login": ORG},
                "html_url": f"https://github.com/{ORG}/{name}", "archived": False, "default_branch": "main",
                "delete_branch_on_merge": False, "branches": {"dev", "main"}, "variables": {}, "files": {},
                "collaborators": set(), "oidc": {"use_default": True}, "runs": {}}
        repo.update(extra)
        self.repos[name] = repo
        return repo

    @staticmethod
    def _public(repo):
        return {k: repo[k] for k in ("id", "name", "owner", "html_url", "archived", "default_branch",
                                     "delete_branch_on_merge")}

    def __call__(self, method, url, headers, body):
        path = url.removeprefix("https://api.github.com")
        data = json.loads(body) if body else None
        self.calls.append((method, path))
        out = self.handle(method, path.split("?")[0], path, data)
        status, payload = out
        return status, json.dumps(payload).encode() if payload is not None else b""

    def handle(self, method, path, full, data):
        if path == f"/orgs/{ORG}/installation":
            return 200, {"id": 42}
        if path == "/app/installations/42/access_tokens":
            return 201, {"token": "ghs_test"}
        if path == f"/repos/{ORG}/site-template/generate":
            self._repo(data["name"], private=data["private"])
            return 201, {"name": data["name"]}
        parts = path.split("/")
        name = parts[3]
        repo = self.repos.get(name)
        rest = "/".join(parts[4:])
        if repo is None:
            return 404, {"message": "Not Found"}
        if rest == "":
            if method == "PATCH":
                repo.update(data)
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
            f = repo["files"].get("booqy.json")
            if method == "GET":
                return (200, f) if f else (404, {"message": "Not Found"})
            if f and data.get("sha") != f["sha"]:
                return 409, {"message": "sha mismatch"}
            repo["files"]["booqy.json"] = {"content": data["content"], "sha": f"sha{len(self.calls)}"}
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

    def booqy_json(self, name):
        return json.loads(base64.b64decode(self.repos[name]["files"]["booqy.json"]["content"]))


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
        "PREVIEW_DOMAIN": "" if environment == "prod" else "dev.booqy.se",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_x", "TURNSTILE_SITE_KEY": "0x4AAA",
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
        for tid, slug in ((TENANT, "test-roma"), (OTHER, "test-other"), (PLAIN, "roma")):
            table.put_item(Item={"PK": f"TENANT#{tid}", "SK": "PROFILE", "slug": slug, "name": "Roma",
                                 "status": "active", "primaryDomain": f"{slug}.dev.booqy.se"})
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sm = boto3.client("secretsmanager", region_name=REGION)
        for environment in ("dev", "prod"):
            sm.create_secret(Name=f"booqy/{environment}/github-app-sites",
                             SecretString=json.dumps({"app_id": "123", "private_key": key}))
        iam = boto3.client("iam", region_name=REGION)
        for prefix in ("dev-", ""):
            iam.create_policy(PolicyName=f"{prefix}booqy-sites-deploy-boundary", Path="/booqy-sites/",
                              PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                                  {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
        fake = FakeGitHub()
        fake._repo("site-template")

        def use(environment):
            for k, v in _env_for(environment).items():
                monkeypatch.setenv(k, v)
            fn = load_fn("site-repo", f"site_repo_{environment}")
            fn._token["value"] = None
            real = fn.GitHub
            monkeypatch.setattr(fn, "GitHub", lambda token, transport=None: real(token, fake))
            # moto has no policy simulator: answer like IAM does for the
            # role's own policy (boundary with the tenantId tag).
            monkeypatch.setattr(fn, "_check_isolation", _simulated_isolation(fn))
            monkeypatch.setattr(fn, "_dev_link", lambda task: links.append(task) or {"tenantId": "t-dev-roma"})
            return fn

        links = []
        yield {"use": use, "github": fake, "table": table, "iam": iam, "links": links}


def _simulated_isolation(fn):
    real = fn._check_isolation

    def check(role_arn, tenant_id):
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
            real(role_arn, tenant_id)
        finally:
            if original:
                iam.simulate_principal_policy = original
    return check


def test_dev_creates_the_repo_from_the_template_and_deploys(world):
    fn = world["use"]("dev")
    out = fn.handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    repo = gh.repos["site-test-roma"]
    assert out == {"status": "ready", "repo": "site-test-roma", "repoUrl": f"https://github.com/{ORG}/site-test-roma"}
    assert repo["private"] is True and repo["default_branch"] == "dev" and repo["branches"] == {"dev", "main"}
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
    site = world["table"].get_item(Key={"PK": f"TENANT#{TENANT}", "SK": "SITE"})["Item"]
    assert site["status"] == "ready" and site["repo"] == "site-test-roma" and "lastError" not in site


def test_the_deploy_role_writes_only_its_own_folder_from_its_own_branch(world):
    fn = world["use"]("dev")
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
                              f"repo:{ORG}@307760488/site-test-roma@{repo_id}:ref:refs/heads/dev"}}}]
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
    assert "previewUrl" not in doc["environments"]["prod"]
    trust = world["iam"].get_role(RoleName="site-test-roma")["Role"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"].endswith(":ref:refs/heads/main")
    # Prod never deploys from onboarding: main deploys on merge, behind the domain gate.
    assert gh.dispatches == []


def test_a_retry_changes_nothing_and_deploys_dev_again(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    gh = world["github"]
    before = json.dumps({k: v for k, v in gh.repos["site-test-roma"].items() if k != "collaborators"}, default=sorted)
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    after = json.dumps({k: v for k, v in gh.repos["site-test-roma"].items() if k != "collaborators"}, default=sorted)
    assert before == after
    assert ("POST", f"/repos/{ORG}/site-template/generate") in gh.calls
    assert sum(1 for c in gh.calls if c == ("POST", f"/repos/{ORG}/site-template/generate")) == 1
    assert gh.dispatches == [("site-test-roma", "dev")]


def test_a_failure_is_recorded_on_the_site_row(world, monkeypatch):
    fn = world["use"]("dev")
    monkeypatch.setattr(fn, "_ensure_oidc_subject", lambda gh, name: (_ for _ in ()).throw(fn.SiteRepoError("boom")))
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
    assert "site-test-roma" in gh.repos                     # archived, never deleted


def test_prod_onboarding_unarchives_an_archived_repo(world):
    fn = world["use"]("dev")
    fn.handler({"action": "provision", "tenantId": TENANT}, None)
    fn.handler({"action": "archive", "tenantId": TENANT}, None)
    assert world["github"].repos["site-test-roma"]["archived"] is True
    world["use"]("prod").handler({"action": "provision", "tenantId": TENANT}, None)
    assert world["github"].repos["site-test-roma"]["archived"] is False


def test_status_invite_and_redeploy(world):
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


def test_dev_only_simulates_a_repo_prod_has_not_created(world):
    fn = world["use"]("dev")
    out = fn.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert out["status"] == "simulated" and out["repo"] == "site-roma"
    assert out["plan"][0].startswith(f"Repo {ORG}/site-roma from site-template")
    assert "site-roma" not in world["github"].repos
    with pytest.raises(world["iam"].exceptions.NoSuchEntityException):
        world["iam"].get_role(RoleName="dev-site-roma")
    status = fn.handler({"action": "status", "tenantId": PLAIN}, None)
    assert status["status"] == "simulated" and status["plan"] == out["plan"]


def test_prod_creates_the_repo_and_asks_dev_for_its_side(world):
    prod = world["use"]("prod")
    out = prod.handler({"action": "provision", "tenantId": PLAIN}, None)
    assert out["status"] == "ready" and "site-roma" in world["github"].repos
    assert world["links"] == [{"action": "mirror", "prodTenantId": PLAIN, "name": "Roma", "slug": "roma"}]
    site = world["table"].get_item(Key={"PK": f"TENANT#{PLAIN}", "SK": "SITE"})["Item"]
    assert site["devTenantId"] == "t-dev-roma"
    # The dev tenant's own onboarding then finds the repo and sets up dev for real.
    world["table"].put_item(Item={"PK": "TENANT#t-dev-roma", "SK": "PROFILE", "slug": "roma", "name": "Roma",
                                  "status": "active"})
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
