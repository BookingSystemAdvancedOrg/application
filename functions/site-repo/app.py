"""site-repo

TRIGGER:
    Direct invoke only (no API Gateway), {"action", "tenantId", ...}:
      tenant-onboarding   provision   after the tenant is active
      tenant-offboarding  archive
      tenant-purge        delete
      platform-tenants    status | provision (Retry) | invite {username} |
                          redeploy {branch} | archive | devDomain {op, host}
                          (Booqy Console, Website tab); delete (Danger
                          zone, Delete customer); dev only: linkProvision
                          {repo} (a site task from prod, below) and
                          testHost {op put|delete, label} (Domains tab)

PURPOSE:
    Every restaurant has its own public website repo site-<slug>, created
    from site-template, that deploys to its own folder through its own
    deploy role. Two kinds of customer (owner 2026-11-15):

    REAL customers, born in the PROD console: repo in GITHUB_ORG
    (BooqyCustomers, App "Booqy Customer Sites"); main deploys to the prod
    account (<slug>.booqy.se), dev to the dev account (<slug>.dev.booqy.se).
    application/docs/orchestrator/CUSTOMER-SITES-PLAN.md (D1),
    infrastructure docs/CUSTOMER-SITE-RUNBOOK.md. Only PROD holds the
    Customer Sites key, so every GitHub write for these repos happens here
    in prod: provision (idempotent, resumable):
      1. the repo: found, unarchived, or generated from the template
         (branches dev and main, default dev);
      2. the repo's OIDC subject: GitHub's default, the immutable one
         (repo:<org>@<org id>/<repo>@<repo id>:ref:...);
      3. the deploy role /booqy-sites/site-<slug>, with the fixed
         permissions boundary and tag tenantId, trusted only for main;
         checked with the IAM policy simulator: may write its own folder,
         not another tenant's;
      4. the dev side's AWS part, through the dev account's
         booqy-sites-prod-link role (it may only invoke dev platform-tenants
         with a site task): an empty dev tenant with the same slug (or the
         existing one) and its dev deploy role, trusted only for the dev
         branch of the repo prod names (owner login and id, repo id); dev
         answers with the role ARN, bucket and its booqy.json section;
      5. repo variables PROD_* and DEV_* (AWS_ROLE_ARN, SITE_BUCKET,
         TENANT_ID, API_BASE_URL; not secrets) and booqy.json's prod and dev
         sections (on dev);
      6. the first prod deploy of main (until one succeeds), and dev's.
    In dev such a tenant (PROFILE siteKind "prod-link") only ever gets AWS
    work: linkProvision from prod writes its role; provision, archive and
    delete never call GitHub (archive and delete remove the dev role; prod
    removes the DEV_* variables). Dev's GitHub client works only for
    TEST_GITHUB_ORG: dev holds the Test Sites key and nothing else.

    TEST customers, created in the DEV console (PROFILE siteKind "test",
    dev only): repo in TEST_GITHUB_ORG (BooqyCustomersDev, App "Booqy Test
    Sites"), generated from SITE_TEMPLATE_OWNER/SITE_TEMPLATE_REPO (the
    public BooqyCustomers/site-template). Both branches deploy in the DEV
    account, to the sandbox bucket TEST_SITES_BUCKET behind the sandbox
    distribution (one standard distribution, noindex):
      main -> main/sites/<tenantId>/, https://<label>.<TEST_MAIN_DOMAIN>
      dev  -> dev/sites/<tenantId>/,  https://<label>.<TEST_DEV_DOMAIN>
    Its CloudFront Function maps the host's label to the tenant through the
    key value store TEST_SITES_KVS_ARN (label -> tenantId): the slug, put at
    provision, and each mapped own domain (example-se for example.se), put
    and deleted by testHost; archive and delete remove them all. A label is
    written only for the tenant that holds its DOMAIN#<label>.<main zone>
    claim (or, removing, when nobody holds it).
    Two roles /booqy-test-sites/test-site-<slug>-main and -dev (boundary
    TEST_DEPLOY_BOUNDARY_ARN, tags tenantId and stage), each trusted for its
    own branch and able to write only its own stage folder; variables
    SANDBOX_MAIN_* and DEV_* (with *_SITE_PREFIX); booqy.json sections
    "test" and "dev" on both branches; the first deploy of each branch.

    A dev tenant that is neither (created in dev before 2026-11-15) is
    "legacy": nothing is set up; re-create it as a test customer.

    Owner guard: a trust is written only for a repo the expected org owns,
    by login and by id: prod GITHUB_ORG (GITHUB_ORG_ID); dev's test
    customers TEST_GITHUB_ORG (TEST_GITHUB_ORG_ID), from GitHub's own
    answer; dev's prod-linked tenants GITHUB_ORG (GITHUB_ORG_ID required),
    only through linkProvision with the repo id prod supplies. Prod refuses
    test customers and any TEST_* setting.

    archive: the roles and variables of this environment go (prod also asks
    dev to remove its role, and removes DEV_*); the repo is archived once no
    environment uses it. delete (a deleted customer, or its data purged
    after the retention period): archive first, then the repo is deleted
    for good. Only prod deletes a real customer's repo; dev deletes its test
    customers' repos. Needs the App's Administration: Read and write; a
    refusal is raised with that hint (the callers record it and carry on).
    devDomain (prod only): the Website tab's dev row, through the same link:
    op "status" (dev's slug address and dev.<domain>'s state) or "mount"
    (dev's domain-attach for dev.<domain>).

    Only repos named site-* are ever touched (every repo name is checked
    here).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, GITHUB_ORG, GITHUB_ORG_ID (prod
    optional, dev required for prod-linked tenants), GITHUB_APP_SECRET_ID,
    SITE_TEMPLATE_REPO, SITE_TEMPLATE_OWNER (optional, default the repo's
    own org), SITES_BUCKET, DEPLOY_ROLE_PATH, DEPLOY_ROLE_PREFIX,
    DEPLOY_BOUNDARY_ARN, OIDC_PROVIDER_ARN, API_BASE_URL,
    MENU_IMAGE_BASE_URL, PREVIEW_DOMAIN, STRIPE_PUBLISHABLE_KEY,
    TURNSTILE_SITE_KEY; prod only: DEV_LINK_ROLE_ARN,
    DEV_PLATFORM_TENANTS_FUNCTION_ARN; dev only (test customers):
    TEST_GITHUB_ORG, TEST_GITHUB_ORG_ID, TEST_SITES_BUCKET,
    TEST_DEPLOY_ROLE_PATH, TEST_DEPLOY_ROLE_PREFIX, TEST_DEPLOY_BOUNDARY_ARN,
    TEST_MAIN_DOMAIN, TEST_DEV_DOMAIN, TEST_SITES_KVS_ARN.

    The App key secret is JSON {"app_id", "private_key"}: prod the Customer
    Sites App, dev the Test Sites App (booqy/dev/github-app-sites).

AWS RESOURCE ACCESS:
    Tenant table GetItem/PutItem/UpdateItem (PROFILE and DOMAIN# claims
    read, SITE row); the GitHub App secret; IAM on role/booqy-sites/* (and,
    dev, role/booqy-test-sites/*) only (create and put policy only with the
    boundary; never remove the boundary); prod: sts:AssumeRole on the dev
    account's booqy-sites-prod-link role; dev: cloudfront-keyvaluestore
    DescribeKeyValueStore, PutKey, DeleteKey on TEST_SITES_KVS_ARN only.
"""

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone

import boto3
import jwt
from botocore.exceptions import ClientError

log = logging.getLogger("site-repo")
log.setLevel(logging.INFO)

GITHUB_API = "https://api.github.com"
REPO_PREFIX = "site-"
_REPO = re.compile(r"site-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
WORKFLOW = "deploy.yml"
_FINISHED_TENANT = ("offboarding", "offboarded")
LINK_ACTOR = "prod-site-repo"

_clients = {}
_tokens = {}            # org (lower case) -> {"value", "expires"}


class SiteRepoError(Exception):
    """A refusal or a GitHub/AWS problem, with a message for the operator."""


def client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def _env():
    return os.environ["ENVIRONMENT"]


def _branch():
    return "main" if _env() == "prod" else "dev"


def _var_prefix():
    return "PROD_" if _env() == "prod" else "DEV_"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_name(slug):
    """site-<slug>, refused unless it is a site-* repo name."""
    name = f"{REPO_PREFIX}{slug}"
    if not isinstance(slug, str) or not _REPO.fullmatch(name):
        raise SiteRepoError(f"refused: {name!r} is not a site-* repository name")
    return name


# ---------------------------------------------------------------------- mode
# One deploy target of a repo: the branch, its repo variables, its folder
# in the bucket, its booqy.json section and (test customers) its host and
# role name suffix.
Stage = namedtuple("Stage", "branch vars folder section domain suffix")


class Mode:
    """Where a tenant's site lives: the real site org (one stage: this
    environment's branch) or, for a dev console test customer, the test org
    and the sandbox (two stages, main and dev, both in this account)."""

    def __init__(self, profile):
        test = profile.get("siteKind") == "test"
        if _env() == "prod":
            if test:
                raise SiteRepoError("refused: a test customer in prod")
            if os.environ.get("TEST_GITHUB_ORG"):
                raise SiteRepoError("refused: TEST_* settings in prod")
        self.test = test
        if test:
            org = os.environ.get("TEST_GITHUB_ORG", "")
            if not org:
                raise SiteRepoError("test sites are not switched on in this environment (TEST_GITHUB_ORG)")
            if org.lower() == os.environ["GITHUB_ORG"].lower():
                raise SiteRepoError("refused: TEST_GITHUB_ORG is the real customers' org")
            self.org, self.org_id = org, os.environ.get("TEST_GITHUB_ORG_ID", "")
            self.bucket = os.environ["TEST_SITES_BUCKET"]
            self.role_path = os.environ["TEST_DEPLOY_ROLE_PATH"]
            self.role_prefix = os.environ["TEST_DEPLOY_ROLE_PREFIX"]
            self.boundary = os.environ["TEST_DEPLOY_BOUNDARY_ARN"]
            self.kvs = os.environ["TEST_SITES_KVS_ARN"]
            self.stages = (
                Stage("dev", "DEV_", "dev/sites", "dev", os.environ["TEST_DEV_DOMAIN"], "dev"),
                Stage("main", "SANDBOX_MAIN_", "main/sites", "test", os.environ["TEST_MAIN_DOMAIN"], "main"),
            )
        else:
            self.org, self.org_id = os.environ["GITHUB_ORG"], os.environ.get("GITHUB_ORG_ID", "")
            self.bucket = os.environ["SITES_BUCKET"]
            self.role_path = os.environ["DEPLOY_ROLE_PATH"]
            self.role_prefix = os.environ["DEPLOY_ROLE_PREFIX"]
            self.boundary = os.environ["DEPLOY_BOUNDARY_ARN"]
            self.kvs = ""
            self.stages = (Stage(_branch(), _var_prefix(), "sites", _env(), os.environ.get("PREVIEW_DOMAIN", ""), ""),)

    @property
    def kind(self):
        return "test" if self.test else "site"

    @property
    def aws_only(self):
        """Dev, a real customer's repo: dev has no key for its org, so only
        the AWS side is done here; prod writes everything on GitHub."""
        return _env() != "prod" and not self.test

    def stage(self, branch):
        found = next((s for s in self.stages if s.branch == branch), None)
        if not found:
            raise SiteRepoError(f"branch must be one of {', '.join(s.branch for s in self.stages)}")
        return found


def _linked_from_prod(profile):
    return profile.get("siteKind") == "prod-link" or profile.get("createdBy") == LINK_ACTOR


def _legacy(profile, row):
    """A dev tenant that is neither a test customer nor linked from prod.
    A dev tenant prod adopted before siteKind existed is recognised by the
    dev side it already has (any SITE status but simulated) on a repo not
    named site-test-* (those dev made alone)."""
    if _env() == "prod" or profile.get("siteKind") == "test" or _linked_from_prod(profile):
        return False
    adopted = row.get("status") not in (None, "simulated", "legacy") and \
        not str(profile.get("slug") or "").startswith("test-")
    return not adopted


LEGACY_MESSAGE = ("Created in dev before test sites: no site repo is set up for it. Re-create it as a test "
                  "customer (Delete customer, then New customer).")


# --------------------------------------------------------------------- GitHub
def _urllib(method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https host
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class GitHub:
    def __init__(self, token, transport=None):
        self._auth = f"Bearer {token}"
        self._transport = transport or _urllib

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        headers = {"Authorization": self._auth, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "booqy-site-repo"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, GITHUB_API + path, headers, data)
        payload = json.loads(raw) if raw else None
        if status not in ok:
            message = (payload or {}).get("message") if isinstance(payload, dict) else None
            raise SiteRepoError(f"GitHub {method} {path}: {status} {message or ''}".strip())
        return status, payload


def _app_jwt(app_id, private_key, now):
    # GitHub accepts at most 10 minutes; iat a minute back for clock drift.
    return jwt.encode({"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(app_id)}, private_key,
                      algorithm="RS256")


def _github_org():
    """The one org this environment's App key is for: prod GITHUB_ORG
    (Customer Sites), dev TEST_GITHUB_ORG (Test Sites). Dev never calls
    GitHub for a real customer's repo."""
    if _env() == "prod":
        return os.environ["GITHUB_ORG"]
    org = os.environ.get("TEST_GITHUB_ORG", "")
    if not org:
        raise SiteRepoError("refused: dev calls GitHub only for test customers (TEST_GITHUB_ORG is not set)")
    return org


def github(transport=None, mode=None):
    """A client with an installation token of this environment's App ("Booqy
    Customer Sites" in prod, "Booqy Test Sites" in dev), cached until five
    minutes before it expires. Refused for any other org. The key never
    leaves this function: it is neither logged nor returned."""
    org = mode.org if mode else _github_org()
    if org.lower() != _github_org().lower():
        raise SiteRepoError(f"refused: no GitHub calls for {org} in {_env()}")
    cached = _tokens.get(org.lower())
    now = time.time()
    if not cached or now > cached["expires"]:
        secret = json.loads(client("secretsmanager").get_secret_value(
            SecretId=os.environ["GITHUB_APP_SECRET_ID"])["SecretString"])
        app = GitHub(_app_jwt(secret["app_id"], secret["private_key"], now), transport)
        _, installation = app.call("GET", f"/orgs/{org}/installation")
        _, issued = app.call("POST", f"/app/installations/{installation['id']}/access_tokens")
        cached = _tokens[org.lower()] = {"value": issued["token"], "expires": now + 55 * 60 - 5 * 60}
    return GitHub(cached["value"], transport)


def _repo_path(name, rest="", org=None):
    if not _REPO.fullmatch(name):
        raise SiteRepoError(f"refused: {name!r} is not a site-* repository name")
    return f"/repos/{org or os.environ['GITHUB_ORG']}/{name}{rest}"


# ------------------------------------------------------------------- DynamoDB
def _table():
    return boto3.resource("dynamodb").Table(os.environ["TENANT_TABLE_NAME"])


def _profile(tenant_id):
    item = _table().get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}, ConsistentRead=True).get("Item")
    if not item:
        raise SiteRepoError(f"tenant {tenant_id} not found")
    return item


def _site_row(tenant_id):
    return _table().get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "SITE"}, ConsistentRead=True).get("Item") or {}


def _save_site(tenant_id, fields, remove=()):
    names, values, sets = {}, {":now": _now()}, ["updatedAt = :now"]
    for i, (key, value) in enumerate(fields.items()):
        names[f"#f{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    expression = "SET " + ", ".join(sets)
    if remove:
        expression += " REMOVE " + ", ".join(remove)
    _table().update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "SITE"}, UpdateExpression=expression,
                         ExpressionAttributeNames=names, ExpressionAttributeValues=values)


# ---------------------------------------------------------------------- repo
def _ensure_repo(gh, name, profile, mode):
    org = mode.org
    if mode.aws_only:
        raise SiteRepoError(f"refused: dev never calls GitHub for {org}/{name} (set up from the production console)")
    status, repo = gh.call("GET", _repo_path(name, org=org), ok=(200, 404))
    if status == 404:
        template_owner = os.environ.get("SITE_TEMPLATE_OWNER") or org
        template = os.environ["SITE_TEMPLATE_REPO"]
        # A test customer gets only the template's dev branch, and main is
        # made from it: the template's main is what real customers run in
        # prod, and its Deploy workflow knows nothing of the sandbox
        # (SANDBOX_MAIN_*), so a main copied from it was never deployed.
        gh.call("POST", f"/repos/{template_owner}/{template}/generate", {
            "owner": org, "name": name, "private": False, "include_all_branches": not mode.test,
            "description": f"Webbplats för {profile.get('name') or profile['slug']} (Booqy"
                           f"{', testkund' if mode.test else ''})",
        })
        repo = _wait_for_branch(gh, name, "dev", org=org)
        if mode.test:
            _main_from_dev(gh, name, org)
    elif repo.get("archived"):
        _, repo = gh.call("PATCH", _repo_path(name, org=org), {"archived": False})
    if repo.get("private"):                 # site repos are public (owner 2026-11-15): no secrets, free Actions minutes
        _, repo = gh.call("PATCH", _repo_path(name, org=org), {"visibility": "public"})
    if repo.get("default_branch") != "dev" or repo.get("delete_branch_on_merge"):
        _, repo = gh.call("PATCH", _repo_path(name, org=org), {"default_branch": "dev", "delete_branch_on_merge": False})
    return repo


def _main_from_dev(gh, name, org):
    """A new test repo's main branch, at dev's first commit."""
    _, ref = gh.call("GET", _repo_path(name, "/git/ref/heads/dev", org=org))
    gh.call("POST", _repo_path(name, "/git/refs", org=org),
            {"ref": "refs/heads/main", "sha": ref["object"]["sha"]}, ok=(201, 422))
    _wait_for_branch(gh, name, "main", org=org)


def _wait_for_branch(gh, name, branch, attempts=20, delay=1.5, org=None):
    # A generated repo exists at once but gets its branches a moment later.
    for _ in range(attempts):
        status, _ = gh.call("GET", _repo_path(name, f"/branches/{branch}", org=org), ok=(200, 404))
        if status == 200:
            return gh.call("GET", _repo_path(name, org=org))[1]
        time.sleep(delay)
    raise SiteRepoError(f"{name}: branch {branch} did not appear after generating from the template")


def _ensure_oidc_subject(gh, name, org=None):
    """GitHub's default subject, which in these organisations is the
    immutable one (owner and repo ids in it): a repo deleted and re-created
    under the same name can't use the old role. Resets an older custom
    template."""
    _, current = gh.call("GET", _repo_path(name, "/actions/oidc/customization/sub", org=org), ok=(200, 404))
    if current and not current.get("use_default", True):
        gh.call("PUT", _repo_path(name, "/actions/oidc/customization/sub", org=org), {"use_default": True})


def oidc_subject(repo, branch):
    """repo:<org>@<org id>/<repo>@<repo id>:ref:refs/heads/<branch>, the
    immutable subject (the platform's own roles match the same format)."""
    return (f"repo:{repo['owner']['login']}@{repo['owner']['id']}/{repo['name']}@{repo['id']}"
            f":ref:refs/heads/{branch}")


# ---------------------------------------------------------------------- IAM
def role_name(slug, mode=None, stage=None):
    prefix = mode.role_prefix if mode else os.environ["DEPLOY_ROLE_PREFIX"]
    name = f"{prefix}{slug}" + (f"-{stage.suffix}" if stage and stage.suffix else "")
    if len(name) > 64:
        raise SiteRepoError(f"role name {name} is longer than 64 characters")
    return name


def _check_owner(repo, mode=None):
    """A trust is written only for a repo the expected org owns (login and,
    when configured, id): prod and dev's prod-linked tenants BooqyCustomers,
    dev's test customers BooqyCustomersDev, never across. GitHub follows a
    transferred repo's old path, so a GET can answer with a repo another
    organisation owns; its token must never get a deploy role. Logins are
    case-insensitive; the subject keeps GitHub's exact casing."""
    org = mode.org if mode else os.environ["GITHUB_ORG"]
    org_id = mode.org_id if mode else os.environ.get("GITHUB_ORG_ID", "")
    owner = repo.get("owner") or {}
    login = owner.get("login") or ""
    if login.lower() != org.lower():
        raise SiteRepoError(f"refused: {repo.get('name')!r} owner is not {org} ({login or 'unknown'})")
    if org_id and str(owner.get("id")) != str(org_id):
        raise SiteRepoError(f"refused: {repo.get('name')!r} owner id is not {org_id} ({owner.get('id')})")


def trust_policy(repo, mode=None, stage=None):
    _check_owner(repo, mode)
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": os.environ["OIDC_PROVIDER_ARN"]},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": oidc_subject(repo, stage.branch if stage else _branch()),
            }},
        }],
    }


def _folder(mode, stage, tenant_id):
    return f"{stage.folder}/{tenant_id}"


def deploy_policy(tenant_id, mode, stage):
    bucket = mode.bucket
    folder = _folder(mode, stage, tenant_id)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "OwnSiteFolder", "Effect": "Allow",
             "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
             "Resource": f"arn:aws:s3:::{bucket}/{folder}/*"},
            {"Sid": "ListOwnSiteFolder", "Effect": "Allow", "Action": "s3:ListBucket",
             "Resource": f"arn:aws:s3:::{bucket}",
             "Condition": {"StringLike": {"s3:prefix": f"{folder}/*"}}},
        ],
    }


def _ensure_role(tenant_id, slug, repo, mode, stage):
    """Create first: IAM judges GetRole on a role that doesn't exist yet
    against its name without the path, which this function's policy
    (role/<path>/*) doesn't cover. An existing role's ARN has the path, so
    reading it afterwards is allowed."""
    iam = client("iam")
    name = role_name(slug, mode, stage)
    trust = json.dumps(trust_policy(repo, mode, stage))
    tags = [{"Key": "tenantId", "Value": tenant_id}, {"Key": "repo", "Value": repo["name"]}]
    if mode.test:
        tags.append({"Key": "stage", "Value": stage.branch})
    try:
        role = iam.create_role(
            RoleName=name, Path=mode.role_path, AssumeRolePolicyDocument=trust,
            PermissionsBoundary=mode.boundary, MaxSessionDuration=3600,
            Description=f"Deploys {repo['name']} ({stage.branch}) to {stage.folder}/{tenant_id}/ only",
            Tags=tags,
        )["Role"]
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=name)["Role"]
        found = {t["Key"]: t["Value"] for t in role.get("Tags", [])}
        if found.get("tenantId") != tenant_id:
            raise SiteRepoError(f"role {name} belongs to tenant {found.get('tenantId')}, not {tenant_id}")
        if mode.test and found.get("stage") != stage.branch:
            raise SiteRepoError(f"role {name} is for stage {found.get('stage')}, not {stage.branch}")
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
    iam.put_role_policy(RoleName=name, PolicyName="deploy-own-site",
                        PolicyDocument=json.dumps(deploy_policy(tenant_id, mode, stage)))
    return role["Arn"]


def _isolation_probes(tenant_id, mode, stage):
    bucket = mode.bucket
    probes = {f"arn:aws:s3:::{bucket}/{stage.folder}/{tenant_id}/index.html": "allowed",
              f"arn:aws:s3:::{bucket}/{stage.folder}/isolation-probe-{tenant_id}/index.html": "implicitDeny",
              f"arn:aws:s3:::{bucket}/index.html": "implicitDeny"}
    if mode.test:
        for other in mode.stages:
            if other != stage:
                probes[f"arn:aws:s3:::{bucket}/{other.folder}/{tenant_id}/index.html"] = "implicitDeny"
        probes[f"arn:aws:s3:::{bucket}/_platform/robots.txt"] = "implicitDeny"
        probes[f"arn:aws:s3:::{os.environ['SITES_BUCKET']}/sites/{tenant_id}/index.html"] = "implicitDeny"
    return probes


def _check_isolation(role_arn, tenant_id, mode=None, stage=None):
    """The policy simulator, boundary and tags included: the role may write
    its own folder and nothing in another tenant's (a test role: nor the
    other stage's folder, the sandbox's own files or the real sites
    bucket). One resource per call: the simulator names its result by a
    resource pattern, not by the ARN asked about, so each answer is only
    matched to its own question."""
    mode = mode or _default_mode()
    stage = stage or mode.stages[0]
    context = [{"ContextKeyName": "aws:PrincipalTag/tenantId", "ContextKeyValues": [tenant_id],
                "ContextKeyType": "string"}]
    if mode.test:
        context.append({"ContextKeyName": "aws:PrincipalTag/stage", "ContextKeyValues": [stage.branch],
                        "ContextKeyType": "string"})
    for arn, expected in _isolation_probes(tenant_id, mode, stage).items():
        result = client("iam").simulate_principal_policy(
            PolicySourceArn=role_arn, ActionNames=["s3:PutObject"], ResourceArns=[arn], ContextEntries=context)
        evaluation = result["EvaluationResults"][0]
        specific = evaluation.get("ResourceSpecificResults") or []
        decision = specific[0]["EvalResourceDecision"] if specific else evaluation["EvalDecision"]
        if decision != expected:
            raise SiteRepoError(f"isolation check failed: s3:PutObject on {arn} is {decision}, expected {expected}")


def _default_mode():
    return Mode({})


def _delete_role(slug, mode=None, stage=None):
    """Only called for a role setup recorded (SITE.roleArn / roleArns): IAM
    refuses reads of a missing role here (see _ensure_role) instead of
    NoSuchEntity."""
    iam = client("iam")
    name = role_name(slug, mode, stage)
    try:
        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        iam.delete_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        pass


# ------------------------------------------------------- variables, booqy.json
def site_variables(tenant_id, role_arns, mode=None):
    """role_arns: {branch: arn}; a str for the one stage of a real site."""
    mode = mode or _default_mode()
    if isinstance(role_arns, str):
        role_arns = {mode.stages[0].branch: role_arns}
    out = {}
    for stage in mode.stages:
        p = stage.vars
        out.update({f"{p}AWS_ROLE_ARN": role_arns.get(stage.branch, ""), f"{p}SITE_BUCKET": mode.bucket,
                    f"{p}TENANT_ID": tenant_id, f"{p}API_BASE_URL": os.environ["API_BASE_URL"].rstrip("/")})
        if mode.test:
            out[f"{p}SITE_PREFIX"] = stage.folder
    return out


def _set_variables(gh, name, variables, org=None):
    _, existing = gh.call("GET", _repo_path(name, "/actions/variables?per_page=30", org=org))
    current = {v["name"]: v["value"] for v in existing.get("variables", [])}
    for key, value in variables.items():
        if key not in current:
            gh.call("POST", _repo_path(name, "/actions/variables", org=org), {"name": key, "value": value})
        elif current[key] != value:
            gh.call("PATCH", _repo_path(name, f"/actions/variables/{key}", org=org), {"name": key, "value": value})


def _variable_names(gh, name, org=None):
    _, existing = gh.call("GET", _repo_path(name, "/actions/variables?per_page=30", org=org))
    return {v["name"] for v in existing.get("variables", [])}


def _public_values(tenant_id):
    return {
        "tenantId": tenant_id,
        "apiBaseUrl": os.environ["API_BASE_URL"].rstrip("/"),
        "menuImageBaseUrl": os.environ.get("MENU_IMAGE_BASE_URL", "").rstrip("/"),
        "stripePublishableKey": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "turnstileSiteKey": os.environ.get("TURNSTILE_SITE_KEY", ""),
    }


def environment_config(tenant_id, profile, mode=None, stage=None):
    """One booqy.json section: public values only. Real sites: this
    environment's; test customers: the stage's ("test" = main, "dev")."""
    if mode and mode.test:
        config = {**_public_values(tenant_id), "previewUrl": f"https://{profile['slug']}.{stage.domain}"}
        domain = profile.get("primaryDomain") or ""
        # A mapped own domain (example-se.test.booqy.se), on main only.
        if stage.branch == "main" and domain.endswith(f".{stage.domain}") and \
                domain != f"{profile['slug']}.{stage.domain}":
            config["domain"] = domain
        return config
    preview = os.environ.get("PREVIEW_DOMAIN", "")
    domain = profile.get("primaryDomain") or ""
    if preview and domain.endswith(f".{preview}"):
        domain = ""                                     # the preview, not the restaurant's own domain
    config = _public_values(tenant_id)
    if preview:
        config["previewUrl"] = f"https://{profile['slug']}.{preview}"
    if domain:
        config["domain"] = domain
    return config


def _sections(tenant_id, profile, mode):
    return {stage.section: environment_config(tenant_id, profile, mode, stage) for stage in mode.stages}


def _write_booqy_json(gh, name, tenant_id, profile, mode=None, branch="dev", extra=None):
    """Merges this mode's sections (and extra ones: prod writes dev's) into
    booqy.json on the branch. True when it committed (the push deploys that
    branch)."""
    mode = mode or _default_mode()
    path = _repo_path(name, "/contents/booqy.json", org=mode.org)
    sections = {**_sections(tenant_id, profile, mode), **(extra or {})}
    label = "test and dev" if mode.test else " and ".join(sections)
    for _ in range(3):                                  # another environment may write at the same moment
        status, current = gh.call("GET", f"{path}?ref={branch}", ok=(200, 404))
        doc = json.loads(base64.b64decode(current["content"])) if status == 200 else {}
        updated = {**doc, "slug": profile["slug"], "name": profile.get("name") or profile["slug"],
                   "environments": {**doc.get("environments", {}), **sections}}
        if updated == doc:
            return False
        body = {"message": f"chore: booqy.json for {label} (Booqy)", "branch": branch,
                "content": base64.b64encode((json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode()).decode()}
        if status == 200:
            body["sha"] = current["sha"]
        put_status, _ = gh.call("PUT", path, body, ok=(200, 201, 409, 422))
        if put_status in (200, 201):
            return True
    raise SiteRepoError(f"{name}: booqy.json kept changing, try again")


def _dispatch(gh, name, branch, org=None):
    gh.call("POST", _repo_path(name, f"/actions/workflows/{WORKFLOW}/dispatches", org=org), {"ref": branch})


# ---------------------------------------------------------- dev side (prod)
_MIRROR_FIELDS = ("name", "slug", "ownerEmail", "ownerName", "planId", "legalName", "orgNumber", "contactEmail",
                  "contactPhone", "address")
# The dev side's repo variables, the only ones prod takes from dev's answer.
DEV_VARIABLES = ("DEV_AWS_ROLE_ARN", "DEV_SITE_BUCKET", "DEV_TENANT_ID", "DEV_API_BASE_URL")
_ARN = re.compile(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,512}")


def _dev_link(task):
    """Prod -> dev, the only way: assume the dev account's booqy-sites-prod-link
    role (it may only invoke dev platform-tenants) and hand it a site task."""
    creds = client("sts").assume_role(RoleArn=os.environ["DEV_LINK_ROLE_ARN"],
                                      RoleSessionName=f"site-repo-{task['slug']}"[:64])["Credentials"]
    lam = boto3.client("lambda", aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                       aws_session_token=creds["SessionToken"])
    response = lam.invoke(FunctionName=os.environ["DEV_PLATFORM_TENANTS_FUNCTION_ARN"],
                          Payload=json.dumps({"siteTask": task}).encode())
    body = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError") or body.get("error"):
        raise SiteRepoError(f"dev side: {body.get('message') or body.get('errorMessage') or body.get('error')}")
    return body


def _linked():
    return _env() == "prod" and bool(os.environ.get("DEV_LINK_ROLE_ARN"))


def _repo_ref(repo):
    """What dev needs to write the dev role's trust, and nothing more."""
    return {"name": repo["name"], "id": repo["id"], "html_url": repo.get("html_url"),
            "owner": {"login": repo["owner"]["login"], "id": repo["owner"]["id"]}}


def _dev_side(answer):
    """Dev's answer to linkProvision -> (DEV_* variables, its booqy.json
    section). Only the DEV_* names are taken, each a plain value."""
    site = answer.get("site") if isinstance(answer.get("site"), dict) else {}
    variables = site.get("variables") if isinstance(site.get("variables"), dict) else {}
    out = {k: variables[k] for k in DEV_VARIABLES if isinstance(variables.get(k), str) and variables[k]}
    config = site.get("config")
    if set(out) != set(DEV_VARIABLES) or not _ARN.fullmatch(out["DEV_AWS_ROLE_ARN"]) or not isinstance(config, dict):
        raise SiteRepoError("dev side: no deploy role in dev's answer")
    return out, config


# ------------------------------------------------------- sandbox host map (dev)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _claim_holder(label):
    """Who holds DOMAIN#<label>.<main zone> (the claim onboarding or the
    Domains tab writes before a label is mapped), or None."""
    item = _table().get_item(Key={"PK": f"DOMAIN#{label}.{os.environ['TEST_MAIN_DOMAIN']}", "SK": "TENANT"},
                             ConsistentRead=True).get("Item")
    return (item or {}).get("tenantId")


def _kvs_write(mode, op, label, value=None):
    """PutKey / DeleteKey with the store's current ETag, retried when another
    writer got in between. A delete of a missing key is done already."""
    kvs = client("cloudfront-keyvaluestore")
    for _ in range(5):
        etag = kvs.describe_key_value_store(KvsARN=mode.kvs)["ETag"]
        try:
            if op == "put":
                kvs.put_key(KvsARN=mode.kvs, Key=label, Value=value, IfMatch=etag)
            else:
                kvs.delete_key(KvsARN=mode.kvs, Key=label, IfMatch=etag)
            return True
        except ClientError as exc:
            code = exc.response["Error"].get("Code")
            if op == "delete" and code == "ResourceNotFoundException":
                return False
            if code not in ("ConflictException", "PreconditionFailed"):
                raise
    raise SiteRepoError("the test sites host map kept changing, try again")


def _map_label(tenant_id, mode, label):
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise SiteRepoError(f"refused: {label!r} is not a host label")
    holder = _claim_holder(label)
    if holder != tenant_id:
        raise SiteRepoError(f"refused: {label}.{os.environ['TEST_MAIN_DOMAIN']} is not claimed by {tenant_id}")
    _kvs_write(mode, "put", label, tenant_id)
    _table().update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "SITE"}, UpdateExpression="ADD testHosts :l",
                         ExpressionAttributeValues={":l": {label}})


def _unmap_label(tenant_id, mode, label):
    """Only a label this tenant holds, or one nobody holds any more
    (offboarding released the claims first)."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise SiteRepoError(f"refused: {label!r} is not a host label")
    holder = _claim_holder(label)
    if holder not in (None, tenant_id):
        log.info(json.dumps({"action": "unmap", "tenantId": tenant_id, "label": label, "skipped": "claimed by another"}))
    else:
        _kvs_write(mode, "delete", label)
    _table().update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "SITE"}, UpdateExpression="DELETE testHosts :l",
                         ExpressionAttributeValues={":l": {label}})


def _unmap_all(tenant_id, mode, slug, row):
    for label in sorted({slug, *(row.get("testHosts") or ())}):
        _unmap_label(tenant_id, mode, label)


def test_host(tenant_id, op, label):
    """The Domains tab (dev): a test customer's mapped own domain, label
    example-se for example.se, on both its sandbox hosts."""
    profile = _profile(tenant_id)
    mode = Mode(profile)
    if not mode.test:
        raise SiteRepoError("refused: only a test customer has sandbox hosts")
    if op == "put":
        if profile.get("status") in _FINISHED_TENANT:
            raise SiteRepoError(f"tenant {tenant_id} is {profile.get('status')}")
        _map_label(tenant_id, mode, label)
    elif op == "delete":
        if label == profile.get("slug"):
            raise SiteRepoError("refused: the slug's own host goes only with the customer")
        _unmap_label(tenant_id, mode, label)
    else:
        raise SiteRepoError(f"unknown testHost op {op!r}")
    log.info(json.dumps({"action": "testHost", "op": op, "tenantId": tenant_id, "label": label}))
    return {"label": label, "op": op, "result": "done"}


# ------------------------------------------------------------------- actions
def _aws_only_provision(tenant_id, profile, name, row):
    """Dev, a real customer's dev tenant, from its own onboarding or Retry:
    nothing to do without prod (linkProvision writes the role)."""
    if row.get("status") == "ready" and row.get("roleArn"):
        return {"status": "ready", "repo": name, "repoUrl": row.get("repoUrl"), "managedBy": "prod"}
    message = "Set up from the production console (Run setup again there)"
    log.info(json.dumps({"action": "provision", "tenantId": tenant_id, "repo": name, "waitsFor": "prod"}))
    return {"status": row.get("status") or "waiting", "repo": name, "managedBy": "prod", "message": message}


def provision(tenant_id, gh=None):
    profile = _profile(tenant_id)
    if profile.get("status") in _FINISHED_TENANT:
        raise SiteRepoError(f"tenant {tenant_id} is {profile.get('status')}")
    name = repo_name(profile.get("slug"))
    row = _site_row(tenant_id)
    if _legacy(profile, row):
        _save_site(tenant_id, {"status": "legacy", "repo": name, "lastError": LEGACY_MESSAGE}, remove=("dryRun",))
        log.info(json.dumps({"action": "provision", "tenantId": tenant_id, "repo": name, "legacy": True}))
        return {"status": "legacy", "repo": name, "message": LEGACY_MESSAGE}
    mode = Mode(profile)
    if mode.aws_only:
        return _aws_only_provision(tenant_id, profile, name, row)
    gh = gh or github(mode=mode)
    _save_site(tenant_id, {"status": "creating", "repo": name, "kind": mode.kind, "org": mode.org}, remove=("dryRun",))
    dev = None
    role_arns = {}
    try:
        if mode.test:
            _map_label(tenant_id, mode, profile["slug"])        # <slug>.test. and .test-dev. answer at once
        repo = _ensure_repo(gh, name, profile, mode)
        _ensure_oidc_subject(gh, name, mode.org)
        for stage in mode.stages:
            role_arns[stage.branch] = _ensure_role(tenant_id, profile["slug"], repo, mode, stage)
            _check_isolation(role_arns[stage.branch], tenant_id, mode, stage)
        variables = site_variables(tenant_id, role_arns, mode)
        dev_section = None
        if _linked():
            # Dev's AWS side first (its role trusts this repo's dev branch);
            # its variables and booqy.json section are written here, with
            # prod's key: dev has none for this org.
            dev = _dev_link({"action": "mirror", "prodTenantId": tenant_id, "repo": _repo_ref(repo),
                             **{k: profile[k] for k in _MIRROR_FIELDS if profile.get(k) not in (None, "")}})
            dev_variables, dev_section = _dev_side(dev)
            variables.update(dev_variables)
        _set_variables(gh, name, variables, mode.org)
        if mode.test:
            # booqy.json on both branches (each commit deploys its branch),
            # so main's first build already has its section.
            for stage in mode.stages:
                committed = _write_booqy_json(gh, name, tenant_id, profile, mode, stage.branch)
                if not committed and (stage.branch == "dev" or _needs_first_deploy(gh, name, "main", mode.org)):
                    _dispatch(gh, name, stage.branch, mode.org)
        else:
            committed = _write_booqy_json(gh, name, tenant_id, profile, mode,
                                          extra={"dev": dev_section} if dev_section else None)
            if dev_section and not committed:
                _dispatch(gh, name, "dev")              # the booqy.json commit deploys dev otherwise
            if _needs_first_deploy(gh, name):
                _dispatch(gh, name, "main")             # live on <slug>.booqy.se, no domain needed
    except Exception as exc:
        _save_site(tenant_id, {"status": "failed", "lastError": str(exc)[:1000],
                               **({"roleArns": role_arns} if mode.test and role_arns else {})})
        raise
    fields = {"status": "ready", "repoUrl": repo["html_url"], "repoId": repo["id"]}
    if mode.test:
        fields["roleArns"] = role_arns
    else:
        fields["roleArn"] = role_arns[mode.stages[0].branch]
    if dev:
        fields["devTenantId"] = dev.get("tenantId")
    if not _site_row(tenant_id).get("createdAt"):
        fields["createdAt"] = _now()
    _save_site(tenant_id, fields, remove=("lastError",))
    log.info(json.dumps({"action": "provision", "tenantId": tenant_id, "repo": name, "kind": mode.kind}))
    return {"status": "ready", "repo": name, "repoUrl": repo["html_url"]}


def link_provision(tenant_id, repo):
    """Dev: the dev side of a real customer's repo, asked by prod through the
    link (platform-tenants' site task). Prod names the repo (owner login and
    id, repo id), since dev can't read BooqyCustomers. Writes the dev role
    (trusted only for that repo's dev branch), checks it, and answers with
    what prod writes on GitHub: DEV_* variables and booqy.json's dev
    section. No GitHub call."""
    if _env() == "prod":
        raise SiteRepoError("refused: linkProvision is dev's side of a prod site")
    profile = _profile(tenant_id)
    if profile.get("status") in ("offboarding", "purged"):
        raise SiteRepoError(f"tenant {tenant_id} is {profile.get('status')}")
    if not _linked_from_prod(profile):
        raise SiteRepoError("refused: this dev customer is not linked from prod")
    mode = Mode(profile)
    if not mode.org_id:
        raise SiteRepoError("refused: GITHUB_ORG_ID is not set (the owner id must be checked)")
    name = repo_name(profile.get("slug"))
    owner = (repo or {}).get("owner") if isinstance(repo, dict) else None
    if not isinstance(owner, dict) or repo.get("name") != name or not isinstance(repo.get("id"), int) \
            or not isinstance(owner.get("id"), int) or not isinstance(owner.get("login"), str):
        raise SiteRepoError(f"refused: prod must name {name} with its repo id and owner")
    repo = {"name": name, "id": repo["id"], "owner": {"login": owner["login"], "id": owner["id"]}}
    stage = mode.stages[0]
    role_arn = _ensure_role(tenant_id, profile["slug"], repo, mode, stage)   # _check_owner: GITHUB_ORG and its id
    _check_isolation(role_arn, tenant_id, mode, stage)
    fields = {"status": "ready", "repo": name, "kind": mode.kind, "org": mode.org, "repoId": repo["id"],
              "roleArn": role_arn, "managedBy": "prod"}
    fields["repoUrl"] = f"https://github.com/{owner['login']}/{name}"
    if not _site_row(tenant_id).get("createdAt"):
        fields["createdAt"] = _now()
    _save_site(tenant_id, fields, remove=("lastError", "dryRun"))
    log.info(json.dumps({"action": "linkProvision", "tenantId": tenant_id, "repo": name}))
    return {"tenantId": tenant_id, "roleArn": role_arn, "variables": site_variables(tenant_id, role_arn, mode),
            "config": environment_config(tenant_id, profile)}


def archive(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    mode = Mode(profile)
    row = _site_row(tenant_id)
    if mode.test:
        for stage in mode.stages:
            if (row.get("roleArns") or {}).get(stage.branch):
                _delete_role(profile["slug"], mode, stage)
        _unmap_all(tenant_id, mode, profile["slug"], row)
    elif row.get("roleArn"):
        _delete_role(profile["slug"], mode)
    if mode.aws_only:
        # Dev, a real customer's repo: only the role goes here; prod removes
        # the DEV_* variables (its own archive, or Run setup again).
        _save_site(tenant_id, {"status": "archived", "repo": name}, remove=("lastError", "roleArn"))
        log.info(json.dumps({"action": "archive", "tenantId": tenant_id, "repo": name, "awsOnly": True}))
        return {"status": "archived", "repo": name, "repoArchived": False, "managedBy": "prod"}
    gh = gh or github(mode=mode)
    status, repo = gh.call("GET", _repo_path(name, org=mode.org), ok=(200, 404))
    archived = False
    if status == 200 and not repo.get("archived"):
        if _linked():
            _dev_link({"action": "archive", "slug": profile["slug"]})
        mine = set(site_variables(tenant_id, {}, mode)) | (set(DEV_VARIABLES) if _linked() else set())
        for key in sorted(mine & _variable_names(gh, name, mode.org)):
            gh.call("DELETE", _repo_path(name, f"/actions/variables/{key}", org=mode.org))
        if not any(v.endswith("_TENANT_ID") for v in _variable_names(gh, name, mode.org)):
            gh.call("PATCH", _repo_path(name, org=mode.org), {"archived": True})
            archived = True
    _save_site(tenant_id, {"status": "archived", "repo": name}, remove=("lastError", "roleArn", "roleArns"))
    log.info(json.dumps({"action": "archive", "tenantId": tenant_id, "repo": name, "archived": archived}))
    return {"status": "archived", "repo": name, "repoArchived": archived or bool(status == 200 and repo.get("archived"))}


def delete(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    mode = Mode(profile)
    if mode.aws_only:
        archive(tenant_id)
        return {"status": "kept", "repo": name, "repoDeleted": False, "reason": "prod owns this repo"}
    if mode.test:
        _unmap_all(tenant_id, mode, profile["slug"], _site_row(tenant_id))
    gh = gh or github(mode=mode)
    if gh.call("GET", _repo_path(name, org=mode.org), ok=(200, 404))[0] == 404:
        return {"status": "gone", "repo": name, "repoDeleted": False}
    try:
        archive(tenant_id, gh)
        if any(v.endswith("_TENANT_ID") for v in _variable_names(gh, name, mode.org)):
            return {"status": "kept", "repo": name, "repoDeleted": False, "reason": "another environment still uses it"}
        status, body = gh.call("DELETE", _repo_path(name, org=mode.org), ok=(204, 403, 404))
    except SiteRepoError:
        # Delete customer and the purge sweep can delete the same repo at
        # once: a 404 half way means the other one finished it.
        if gh.call("GET", _repo_path(name, org=mode.org), ok=(200, 404))[0] == 404:
            log.info(json.dumps({"action": "delete", "tenantId": tenant_id, "repo": name, "alreadyGone": True}))
            return {"status": "gone", "repo": name, "repoDeleted": False}
        raise
    if status == 403:
        app = "Booqy Test Sites" if mode.test else "Booqy Customer Sites"
        raise SiteRepoError(f"{name} NOT deleted: GitHub refused ({(body or {}).get('message', '403')}). The {app} "
                            "App needs Repository permission Administration: Read and write; delete the "
                            "archived repo by hand on GitHub meanwhile")
    _save_site(tenant_id, {"status": "deleted", "repo": name, "deletedAt": _now()}, remove=("repoUrl", "repoId"))
    log.info(json.dumps({"action": "delete", "tenantId": tenant_id, "repo": name}))
    return {"status": "deleted", "repo": name, "repoDeleted": status == 204}


def _last_run(gh, name, branch, org=None):
    _, runs = gh.call("GET", _repo_path(name, f"/actions/workflows/{WORKFLOW}/runs?branch={branch}&per_page=1", org=org),
                      ok=(200, 404))
    run = ((runs or {}).get("workflow_runs") or [None])[0]
    if not run:
        return None
    return {"status": run["status"], "conclusion": run.get("conclusion"), "sha": run["head_sha"][:7],
            "at": run.get("run_started_at") or run.get("created_at"), "by": (run.get("actor") or {}).get("login"),
            "event": run.get("event"), "url": run["html_url"]}


def _needs_first_deploy(gh, name, branch="main", org=None):
    """The branch has never deployed (or its last finished deploy failed, as
    every one did behind the old own-domain gate)."""
    run = _last_run(gh, name, branch, org)
    return not run or (run["status"] == "completed" and run["conclusion"] != "success")


def status(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    row = _site_row(tenant_id)
    if _legacy(profile, row):
        return {"repo": name, "status": "legacy", "kind": "legacy", "lastError": LEGACY_MESSAGE,
                "org": os.environ["GITHUB_ORG"], "environment": _env(), "createdAt": row.get("createdAt"),
                "updatedAt": row.get("updatedAt"), "config": environment_config(tenant_id, profile)}
    mode = Mode(profile)
    result = {"repo": name, "status": row.get("status") or "none", "lastError": row.get("lastError"),
              "createdAt": row.get("createdAt"), "updatedAt": row.get("updatedAt"),
              "org": mode.org, "environment": _env(), "kind": mode.kind,
              "config": environment_config(tenant_id, profile, mode, mode.stages[0])}
    if mode.test:
        result["urls"] = {stage.branch: f"https://{profile['slug']}.{stage.domain}" for stage in mode.stages}
    if mode.aws_only:
        # Deploys and the repo's state are on the production console's tab.
        return {**result, "managedBy": "prod", "repoUrl": row.get("repoUrl"), "hasRole": bool(row.get("roleArn"))}
    if row.get("status") in (None, "creating"):
        return result
    gh = gh or github(mode=mode)
    code, repo = gh.call("GET", _repo_path(name, org=mode.org), ok=(200, 404))
    if code == 404:
        return {**result, "repoExists": False}
    branches = {"dev": "dev", "main": "main"} if mode.test else {"dev": "dev", "prod": "main"}
    return {**result, "repoExists": True, "repoUrl": repo["html_url"], "archived": repo.get("archived", False),
            "deploys": {key: _last_run(gh, name, branch, mode.org) for key, branch in branches.items()}}


def _github_mode(profile):
    mode = Mode(profile)
    if mode.aws_only:
        raise SiteRepoError("This customer's repo is managed from the production console")
    return mode


def invite(tenant_id, username, gh=None):
    """A GitHub user gets write access to this one repo (an invitation they
    accept on GitHub). No tokens are shown or stored."""
    if not isinstance(username, str) or not _USERNAME.fullmatch(username.strip().lstrip("@")):
        raise SiteRepoError("not a GitHub username")
    username = username.strip().lstrip("@")
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    mode = _github_mode(profile)
    gh = gh or github(mode=mode)
    code, _ = gh.call("PUT", _repo_path(name, f"/collaborators/{username}", org=mode.org), {"permission": "push"})
    log.info(json.dumps({"action": "invite", "tenantId": tenant_id, "repo": name, "username": username}))
    return {"repo": name, "username": username, "result": "invited" if code == 201 else "already_has_access"}


def redeploy(tenant_id, gh=None, branch=None):
    """Prod: main; a test customer's dev or main (default dev). Dev refuses
    a real customer's repo (managed from prod)."""
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    mode = _github_mode(profile)
    if mode.test:
        branch = mode.stage(branch or "dev").branch
    elif branch not in (None, _branch()):
        raise SiteRepoError(f"only {_branch()} deploys in this environment")
    else:
        branch = _branch()
    gh = gh or github(mode=mode)
    _dispatch(gh, name, branch, mode.org)
    return {"repo": name, "branch": branch, "result": "started"}


def dev_domain(tenant_id, op, host=None):
    """Prod's Website tab, dev row: dev platform-tenants answers for its own
    tenant with this slug (status, or mount = its domain-attach for host)."""
    if not _linked():
        raise SiteRepoError("no dev link in this environment")
    if op not in ("status", "mount"):
        raise SiteRepoError(f"unknown devDomain op {op!r}")
    profile = _profile(tenant_id)
    task = {"action": "domain", "op": op, "slug": profile["slug"]}
    if host:
        task["host"] = host
    return _dev_link(task)


ACTIONS = {"provision": provision, "archive": archive, "delete": delete, "status": status}


def handler(event, context):
    action = event.get("action")
    tenant_id = event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise SiteRepoError("tenantId missing")
    if action == "invite":
        return invite(tenant_id, event.get("username"))
    if action == "devDomain":
        return dev_domain(tenant_id, event.get("op"), event.get("host"))
    try:
        if action == "linkProvision":
            return link_provision(tenant_id, event.get("repo"))
        if action == "testHost":
            return test_host(tenant_id, event.get("op"), event.get("label"))
        if action == "redeploy":
            return redeploy(tenant_id, branch=event.get("branch"))
        if action not in ACTIONS:
            raise SiteRepoError(f"unknown action {action!r}")
        return ACTIONS[action](tenant_id)
    except ClientError as exc:
        raise SiteRepoError(f"AWS: {exc.response['Error'].get('Code')}: {exc.response['Error'].get('Message')}") from exc
