"""Meta Graph API port (CONTRACTS.md 13): ads (feature A) and page/Instagram
posts (feature G). `client()` picks the simulator or the real client from
META_CLIENT; the real one lives in shared/meta_graph_real.py (owned by A).
"""

import hashlib
from dataclasses import dataclass, field

from shared import external_client


class MetaGraphError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Token:
    access_token: str
    scopes: tuple = ()


@dataclass(frozen=True)
class Assets:
    businesses: list = field(default_factory=list)
    ad_accounts: list = field(default_factory=list)   # [{id, name, currency, has_payment_method}]
    pages: list = field(default_factory=list)
    instagram: list = field(default_factory=list)


class MetaGraph:
    def authorize_url(self, state, redirect_uri, scopes): raise NotImplementedError
    def exchange_code(self, code, redirect_uri): raise NotImplementedError
    def list_assets(self, token): raise NotImplementedError
    def upload_image(self, token, ad_account_id, data, name): raise NotImplementedError
    def create_campaign(self, token, ad_account_id, name, objective): raise NotImplementedError

    def create_ad_set(self, token, ad_account_id, campaign_id, name, lifetime_budget_ore, start, end, geo,
                      age_min, dsa_beneficiary, dsa_payor):
        raise NotImplementedError

    def create_creative(self, token, ad_account_id, name, page_id, instagram_id, image_hash, headline,
                        primary_text, link, cta):
        raise NotImplementedError

    def create_ad(self, token, ad_account_id, name, ad_set_id, creative_id): raise NotImplementedError
    def find_by_name(self, token, ad_account_id, kind, name): raise NotImplementedError
    def set_status(self, token, object_id, status): raise NotImplementedError
    def insights(self, token, ad_account_id, campaign_ids, day): raise NotImplementedError
    def preview_url(self, token, ad_id): raise NotImplementedError
    def revoke(self, token): raise NotImplementedError
    def publish_page_photo(self, token, page_id, image_url, caption): raise NotImplementedError
    def create_ig_container(self, token, ig_user_id, image_url, caption): raise NotImplementedError
    def publish_ig_container(self, token, ig_user_id, container_id): raise NotImplementedError
    def recent_page_posts(self, token, page_id, limit): raise NotImplementedError


def _sim_id(kind, name):
    return f"sim-{kind}-{hashlib.sha1(str(name).encode()).hexdigest()[:10]}"


class SimulatedMetaGraph(external_client.Simulated, MetaGraph):
    """Deterministic, in memory. Objects are idempotent by name; every call
    is recorded in `calls`. `missing=("ad_account",)` (or "business",
    "page", "instagram") drops that asset."""

    error = MetaGraphError

    def __init__(self, fail=None, missing=()):
        super().__init__(fail)
        self.missing = set(missing)
        self.objects = {}        # id -> {kind, name, status, ...}
        self.posts = []
        self._published = set()

    def authorize_url(self, state, redirect_uri, scopes):
        self._call("authorize_url", state)
        return f"{redirect_uri}?code=simulated&state={state}"

    def exchange_code(self, code, redirect_uri):
        self._call("exchange_code", code)
        if code != "simulated":
            raise MetaGraphError("invalid_code")
        return Token("sim-token", ("ads_management", "pages_manage_posts"))

    def list_assets(self, token):
        self._call("list_assets")
        def has(kind):
            return kind not in self.missing
        return Assets(
            businesses=[{"id": "sim-business-1", "name": "Simulerat företag"}] if has("business") else [],
            ad_accounts=[{"id": "act_sim1", "name": "Simulerat annonskonto", "currency": "SEK",
                          "has_payment_method": True}] if has("ad_account") else [],
            pages=[{"id": "sim-page-1", "name": "Simulerad sida"}] if has("page") else [],
            instagram=[{"id": "sim-ig-1", "username": "simulerad"}] if has("instagram") else [],
        )

    def _create(self, kind, name, **extra):
        object_id = _sim_id(kind, name)
        self.objects.setdefault(object_id, {"kind": kind, "name": name, "status": "PAUSED", **extra})
        return object_id

    def upload_image(self, token, ad_account_id, data, name):
        self._call("upload_image", name)
        return hashlib.sha1(bytes(data)).hexdigest()

    def create_campaign(self, token, ad_account_id, name, objective):
        self._call("create_campaign", name)
        return self._create("campaign", name, objective=objective)

    def create_ad_set(self, token, ad_account_id, campaign_id, name, lifetime_budget_ore, start, end, geo,
                      age_min, dsa_beneficiary, dsa_payor):
        self._call("create_ad_set", name)
        return self._create("adset", name, campaign_id=campaign_id, budget=lifetime_budget_ore)

    def create_creative(self, token, ad_account_id, name, page_id, instagram_id, image_hash, headline,
                        primary_text, link, cta):
        self._call("create_creative", name)
        return self._create("creative", name)

    def create_ad(self, token, ad_account_id, name, ad_set_id, creative_id):
        self._call("create_ad", name)
        return self._create("ad", name, ad_set_id=ad_set_id)

    def find_by_name(self, token, ad_account_id, kind, name):
        self._call("find_by_name", kind, name)
        object_id = _sim_id(kind, name)
        return object_id if object_id in self.objects else None

    def set_status(self, token, object_id, status):
        self._call("set_status", object_id, status)
        if object_id not in self.objects:
            raise MetaGraphError("not_found")
        self.objects[object_id]["status"] = status

    def insights(self, token, ad_account_id, campaign_ids, day):
        self._call("insights", day)
        rows = []
        for campaign_id in campaign_ids:
            n = int(hashlib.sha1(f"{campaign_id}|{day}".encode()).hexdigest()[:6], 16)
            rows.append({"campaign_id": campaign_id, "day": day, "impressions": 500 + n % 1500,
                         "clicks": 10 + n % 60, "spend_ore": 2000 + n % 8000})
        return rows

    def preview_url(self, token, ad_id):
        self._call("preview_url", ad_id)
        return f"https://example.invalid/preview/{ad_id}"

    def revoke(self, token):
        self._call("revoke")

    def publish_page_photo(self, token, page_id, image_url, caption):
        self._call("publish_page_photo", page_id)
        post_id = _sim_id("post", f"{page_id}|{image_url}|{caption}")
        self.posts.append({"id": post_id, "message": caption,
                           "permalink": f"https://example.invalid/posts/{post_id}"})
        return post_id

    def create_ig_container(self, token, ig_user_id, image_url, caption):
        self._call("create_ig_container", ig_user_id)
        return _sim_id("container", f"{ig_user_id}|{image_url}|{caption}")

    def publish_ig_container(self, token, ig_user_id, container_id):
        self._call("publish_ig_container", container_id)
        if container_id in self._published:
            raise MetaGraphError("already_published")
        self._published.add(container_id)
        return _sim_id("media", container_id)

    def recent_page_posts(self, token, page_id, limit):
        self._call("recent_page_posts", page_id)
        return list(reversed(self.posts))[:limit]


def client(**kwargs) -> MetaGraph:
    if external_client.mode("meta") == external_client.REAL:
        from shared.meta_graph_real import RealMetaGraph
        return RealMetaGraph()
    return SimulatedMetaGraph(**kwargs)
