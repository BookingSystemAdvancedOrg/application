"""site-repo

TRIGGER:
    Direct invoke only (no API Gateway), {"action", "tenantId", ...}:
      tenant-onboarding   provision   after the tenant is active
      tenant-offboarding  archive
      platform-tenants    status | provision (Retry) | invite {username} |
                          redeploy | archive   (Booqy Console, Website tab)

PURPOSE:
    Every restaurant has its own private website repo site-<slug> in the
    GitHub organisation, created from site-template, that deploys to its own
    folder sites/<tenantId>/ of the sites bucket through its own deploy role.
    application/docs/orchestrator/CUSTOMER-SITES-PLAN.md, infrastructure
    docs/CUSTOMER-SITE-RUNBOOK.md.

    provision (idempotent, resumable; dev and prod share the repo, each
    environment writes only its own part):
      1. the repo: found, unarchived, or generated from the template
         (branches dev and main, default dev);
      2. the repo's OIDC subject (repository_owner_id, repository_id, ref):
         site repos can't match the platform's own "repo:..." OIDC roles;
      3. the deploy role /booqy-sites/<env->site-<slug>, with the fixed
         permissions boundary and tag tenantId, trusted only for this repo's
         dev branch (dev) or main branch (prod); checked with the IAM policy
         simulator: may write its own folder, not another tenant's;
      4. repo variables <ENV>_AWS_ROLE_ARN, <ENV>_SITE_BUCKET,
         <ENV>_TENANT_ID, <ENV>_API_BASE_URL (not secrets);
      5. booqy.json on dev: environments.<env> (tenantId, API URL, public
         keys, site URL); the commit deploys dev, otherwise dev is deployed
         with a workflow dispatch.
    archive: this environment's role and variables go; the repo is archived
    once no environment uses it any more. Never deleted.

    Only repos named site-* are ever touched (GitHub App "Booqy Sites" is
    installed on those only, and every repo name is checked here).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, GITHUB_ORG, GITHUB_APP_SECRET_ID,
    SITE_TEMPLATE_REPO, SITES_BUCKET, DEPLOY_ROLE_PATH, DEPLOY_ROLE_PREFIX,
    DEPLOY_BOUNDARY_ARN, OIDC_PROVIDER_ARN, API_BASE_URL, MENU_IMAGE_BASE_URL,
    PREVIEW_DOMAIN, STRIPE_PUBLISHABLE_KEY, TURNSTILE_SITE_KEY

AWS RESOURCE ACCESS:
    Tenant table GetItem/PutItem/UpdateItem (PROFILE read, SITE row);
    the GitHub App secret; IAM on role/booqy-sites/* only (create and
    put policy only with the boundary; never remove the boundary).
"""

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
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
OIDC_CLAIMS = ["repository_owner_id", "repository_id", "ref"]
WORKFLOW = "deploy.yml"
_FINISHED_TENANT = ("offboarding", "offboarded")

_clients = {}
_token = {"value": None, "expires": 0.0}


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


def github(transport=None):
    """A client with an installation token of the "Booqy Sites" App (cached
    until five minutes before it expires). The key never leaves this
    function: it is neither logged nor returned."""
    now = time.time()
    if _token["value"] is None or now > _token["expires"]:
        secret = json.loads(client("secretsmanager").get_secret_value(
            SecretId=os.environ["GITHUB_APP_SECRET_ID"])["SecretString"])
        app = GitHub(_app_jwt(secret["app_id"], secret["private_key"], now), transport)
        _, installation = app.call("GET", f"/orgs/{os.environ['GITHUB_ORG']}/installation")
        _, issued = app.call("POST", f"/app/installations/{installation['id']}/access_tokens")
        _token["value"] = issued["token"]
        _token["expires"] = now + 55 * 60 - 5 * 60
    return GitHub(_token["value"], transport)


def _repo_path(name, rest=""):
    if not _REPO.fullmatch(name):
        raise SiteRepoError(f"refused: {name!r} is not a site-* repository name")
    return f"/repos/{os.environ['GITHUB_ORG']}/{name}{rest}"


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
def _ensure_repo(gh, name, profile):
    status, repo = gh.call("GET", _repo_path(name), ok=(200, 404))
    if status == 404:
        org = os.environ["GITHUB_ORG"]
        template = os.environ["SITE_TEMPLATE_REPO"]
        gh.call("POST", f"/repos/{org}/{template}/generate", {
            "owner": org, "name": name, "private": True, "include_all_branches": True,
            "description": f"Webbplats för {profile.get('name') or profile['slug']} (Booqy)",
        })
        repo = _wait_for_branch(gh, name, "dev")
    elif repo.get("archived"):
        _, repo = gh.call("PATCH", _repo_path(name), {"archived": False})
    if repo.get("default_branch") != "dev" or repo.get("delete_branch_on_merge"):
        _, repo = gh.call("PATCH", _repo_path(name), {"default_branch": "dev", "delete_branch_on_merge": False})
    return repo


def _wait_for_branch(gh, name, branch, attempts=20, delay=1.5):
    # A generated repo exists at once but gets its branches a moment later.
    for _ in range(attempts):
        status, _ = gh.call("GET", _repo_path(name, f"/branches/{branch}"), ok=(200, 404))
        if status == 200:
            return gh.call("GET", _repo_path(name))[1]
        time.sleep(delay)
    raise SiteRepoError(f"{name}: branch {branch} did not appear after generating from the template")


def _ensure_oidc_subject(gh, name):
    _, current = gh.call("GET", _repo_path(name, "/actions/oidc/customization/sub"), ok=(200, 404))
    if not current or current.get("use_default") or current.get("include_claim_keys") != OIDC_CLAIMS:
        gh.call("PUT", _repo_path(name, "/actions/oidc/customization/sub"),
                {"use_default": False, "include_claim_keys": OIDC_CLAIMS})


def oidc_subject(repo, branch):
    return f"repository_owner_id:{repo['owner']['id']}:repository_id:{repo['id']}:ref:refs/heads/{branch}"


# ---------------------------------------------------------------------- IAM
def role_name(slug):
    name = f"{os.environ['DEPLOY_ROLE_PREFIX']}{slug}"
    if len(name) > 64:
        raise SiteRepoError(f"role name {name} is longer than 64 characters")
    return name


def trust_policy(repo):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": os.environ["OIDC_PROVIDER_ARN"]},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": oidc_subject(repo, _branch()),
            }},
        }],
    }


def deploy_policy(tenant_id):
    bucket = os.environ["SITES_BUCKET"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "OwnSiteFolder", "Effect": "Allow",
             "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
             "Resource": f"arn:aws:s3:::{bucket}/sites/{tenant_id}/*"},
            {"Sid": "ListOwnSiteFolder", "Effect": "Allow", "Action": "s3:ListBucket",
             "Resource": f"arn:aws:s3:::{bucket}",
             "Condition": {"StringLike": {"s3:prefix": f"sites/{tenant_id}/*"}}},
        ],
    }


def _ensure_role(tenant_id, slug, repo):
    iam = client("iam")
    name = role_name(slug)
    trust = json.dumps(trust_policy(repo))
    try:
        role = iam.get_role(RoleName=name)["Role"]
        tags = {t["Key"]: t["Value"] for t in role.get("Tags", [])}
        if tags.get("tenantId") != tenant_id:
            raise SiteRepoError(f"role {name} belongs to tenant {tags.get('tenantId')}, not {tenant_id}")
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=name, Path=os.environ["DEPLOY_ROLE_PATH"], AssumeRolePolicyDocument=trust,
            PermissionsBoundary=os.environ["DEPLOY_BOUNDARY_ARN"], MaxSessionDuration=3600,
            Description=f"Deploys {repo['name']} ({_branch()}) to sites/{tenant_id}/ only",
            Tags=[{"Key": "tenantId", "Value": tenant_id}, {"Key": "repo", "Value": repo["name"]}],
        )["Role"]
    iam.put_role_policy(RoleName=name, PolicyName="deploy-own-site", PolicyDocument=json.dumps(deploy_policy(tenant_id)))
    return role["Arn"]


def _check_isolation(role_arn, tenant_id):
    """The policy simulator, boundary and tag included: the role may write
    its own folder and nothing in another tenant's."""
    bucket = os.environ["SITES_BUCKET"]
    probes = {f"arn:aws:s3:::{bucket}/sites/{tenant_id}/index.html": "allowed",
              f"arn:aws:s3:::{bucket}/sites/isolation-probe-{tenant_id}/index.html": "implicitDeny",
              f"arn:aws:s3:::{bucket}/index.html": "implicitDeny"}
    result = client("iam").simulate_principal_policy(
        PolicySourceArn=role_arn, ActionNames=["s3:PutObject"], ResourceArns=list(probes),
        ContextEntries=[{"ContextKeyName": "aws:PrincipalTag/tenantId", "ContextKeyValues": [tenant_id],
                         "ContextKeyType": "string"}])
    for evaluation in result["EvaluationResults"]:
        expected = probes[evaluation["EvalResourceName"]]
        if evaluation["EvalDecision"] != expected:
            raise SiteRepoError(f"isolation check failed: s3:PutObject on {evaluation['EvalResourceName']} is "
                                f"{evaluation['EvalDecision']}, expected {expected}")


def _delete_role(slug):
    iam = client("iam")
    name = role_name(slug)
    try:
        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        iam.delete_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        pass


# ------------------------------------------------------- variables, booqy.json
def site_variables(tenant_id, role_arn):
    p = _var_prefix()
    return {f"{p}AWS_ROLE_ARN": role_arn, f"{p}SITE_BUCKET": os.environ["SITES_BUCKET"],
            f"{p}TENANT_ID": tenant_id, f"{p}API_BASE_URL": os.environ["API_BASE_URL"].rstrip("/")}


def _set_variables(gh, name, variables):
    _, existing = gh.call("GET", _repo_path(name, "/actions/variables?per_page=30"))
    current = {v["name"]: v["value"] for v in existing.get("variables", [])}
    for key, value in variables.items():
        if key not in current:
            gh.call("POST", _repo_path(name, "/actions/variables"), {"name": key, "value": value})
        elif current[key] != value:
            gh.call("PATCH", _repo_path(name, f"/actions/variables/{key}"), {"name": key, "value": value})


def _variable_names(gh, name):
    _, existing = gh.call("GET", _repo_path(name, "/actions/variables?per_page=30"))
    return {v["name"] for v in existing.get("variables", [])}


def environment_config(tenant_id, profile):
    """This environment's part of booqy.json: public values only."""
    preview = os.environ.get("PREVIEW_DOMAIN", "")
    domain = profile.get("primaryDomain") or ""
    if preview and domain.endswith(f".{preview}"):
        domain = ""                                     # the preview, not the restaurant's own domain
    config = {
        "tenantId": tenant_id,
        "apiBaseUrl": os.environ["API_BASE_URL"].rstrip("/"),
        "menuImageBaseUrl": os.environ.get("MENU_IMAGE_BASE_URL", "").rstrip("/"),
        "stripePublishableKey": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "turnstileSiteKey": os.environ.get("TURNSTILE_SITE_KEY", ""),
    }
    if preview:
        config["previewUrl"] = f"https://{profile['slug']}.{preview}"
    if domain:
        config["domain"] = domain
    return config


def _write_booqy_json(gh, name, tenant_id, profile):
    """Merges this environment's section into booqy.json on dev. True when
    it committed (the push deploys dev)."""
    path = _repo_path(name, "/contents/booqy.json")
    for _ in range(3):                                  # another environment may write at the same moment
        status, current = gh.call("GET", f"{path}?ref=dev", ok=(200, 404))
        doc = json.loads(base64.b64decode(current["content"])) if status == 200 else {}
        updated = {**doc, "slug": profile["slug"], "name": profile.get("name") or profile["slug"],
                   "environments": {**doc.get("environments", {}), _env(): environment_config(tenant_id, profile)}}
        if updated == doc:
            return False
        body = {"message": f"chore: booqy.json for {_env()} (Booqy)", "branch": "dev",
                "content": base64.b64encode((json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode()).decode()}
        if status == 200:
            body["sha"] = current["sha"]
        put_status, _ = gh.call("PUT", path, body, ok=(200, 201, 409, 422))
        if put_status in (200, 201):
            return True
    raise SiteRepoError(f"{name}: booqy.json kept changing, try again")


def _dispatch(gh, name, branch):
    gh.call("POST", _repo_path(name, f"/actions/workflows/{WORKFLOW}/dispatches"), {"ref": branch})


# ------------------------------------------------------------------- actions
def provision(tenant_id, gh=None):
    profile = _profile(tenant_id)
    if profile.get("status") in _FINISHED_TENANT:
        raise SiteRepoError(f"tenant {tenant_id} is {profile.get('status')}")
    name = repo_name(profile.get("slug"))
    _save_site(tenant_id, {"status": "creating", "repo": name})
    try:
        gh = gh or github()
        repo = _ensure_repo(gh, name, profile)
        _ensure_oidc_subject(gh, name)
        role_arn = _ensure_role(tenant_id, profile["slug"], repo)
        _check_isolation(role_arn, tenant_id)
        _set_variables(gh, name, site_variables(tenant_id, role_arn))
        committed = _write_booqy_json(gh, name, tenant_id, profile)
        if _env() != "prod" and not committed:
            _dispatch(gh, name, "dev")
    except Exception as exc:
        _save_site(tenant_id, {"status": "failed", "lastError": str(exc)[:1000]})
        raise
    fields = {"status": "ready", "repoUrl": repo["html_url"], "repoId": repo["id"], "roleArn": role_arn}
    if not _site_row(tenant_id).get("createdAt"):
        fields["createdAt"] = _now()
    _save_site(tenant_id, fields, remove=("lastError",))
    log.info(json.dumps({"action": "provision", "tenantId": tenant_id, "repo": name}))
    return {"status": "ready", "repo": name, "repoUrl": repo["html_url"]}


def archive(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    gh = gh or github()
    _delete_role(profile["slug"])
    status, repo = gh.call("GET", _repo_path(name), ok=(200, 404))
    archived = False
    if status == 200 and not repo.get("archived"):
        for key in site_variables(tenant_id, "").keys() & _variable_names(gh, name):
            gh.call("DELETE", _repo_path(name, f"/actions/variables/{key}"))
        if not any(v.endswith("_TENANT_ID") for v in _variable_names(gh, name)):
            gh.call("PATCH", _repo_path(name), {"archived": True})
            archived = True
    _save_site(tenant_id, {"status": "archived", "repo": name}, remove=("lastError", "roleArn"))
    log.info(json.dumps({"action": "archive", "tenantId": tenant_id, "repo": name, "archived": archived}))
    return {"status": "archived", "repo": name, "repoArchived": archived or bool(status == 200 and repo.get("archived"))}


def _last_run(gh, name, branch):
    _, runs = gh.call("GET", _repo_path(name, f"/actions/workflows/{WORKFLOW}/runs?branch={branch}&per_page=1"),
                      ok=(200, 404))
    run = ((runs or {}).get("workflow_runs") or [None])[0]
    if not run:
        return None
    return {"status": run["status"], "conclusion": run.get("conclusion"), "sha": run["head_sha"][:7],
            "at": run.get("run_started_at") or run.get("created_at"), "by": (run.get("actor") or {}).get("login"),
            "event": run.get("event"), "url": run["html_url"]}


def status(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    row = _site_row(tenant_id)
    result = {"repo": name, "status": row.get("status") or "none", "lastError": row.get("lastError"),
              "createdAt": row.get("createdAt"), "updatedAt": row.get("updatedAt"),
              "org": os.environ["GITHUB_ORG"], "environment": _env(),
              "config": environment_config(tenant_id, profile)}
    if row.get("status") in (None, "creating"):
        return result
    gh = gh or github()
    code, repo = gh.call("GET", _repo_path(name), ok=(200, 404))
    if code == 404:
        return {**result, "repoExists": False}
    return {**result, "repoExists": True, "repoUrl": repo["html_url"], "archived": repo.get("archived", False),
            "deploys": {"dev": _last_run(gh, name, "dev"), "prod": _last_run(gh, name, "main")}}


def invite(tenant_id, username, gh=None):
    """A GitHub user gets write access to this one repo (an invitation they
    accept on GitHub). No tokens are shown or stored."""
    if not isinstance(username, str) or not _USERNAME.fullmatch(username.strip().lstrip("@")):
        raise SiteRepoError("not a GitHub username")
    username = username.strip().lstrip("@")
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    gh = gh or github()
    code, _ = gh.call("PUT", _repo_path(name, f"/collaborators/{username}"), {"permission": "push"})
    log.info(json.dumps({"action": "invite", "tenantId": tenant_id, "repo": name, "username": username}))
    return {"repo": name, "username": username, "result": "invited" if code == 201 else "already_has_access"}


def redeploy(tenant_id, gh=None):
    profile = _profile(tenant_id)
    name = repo_name(profile.get("slug"))
    gh = gh or github()
    _dispatch(gh, name, _branch())
    return {"repo": name, "branch": _branch(), "result": "started"}


ACTIONS = {"provision": provision, "archive": archive, "status": status, "redeploy": redeploy}


def handler(event, context):
    action = event.get("action")
    tenant_id = event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise SiteRepoError("tenantId missing")
    if action == "invite":
        return invite(tenant_id, event.get("username"))
    if action not in ACTIONS:
        raise SiteRepoError(f"unknown action {action!r}")
    try:
        return ACTIONS[action](tenant_id)
    except ClientError as exc:
        raise SiteRepoError(f"AWS: {exc.response['Error'].get('Code')}: {exc.response['Error'].get('Message')}") from exc
