"""Social posting: a Facebook channel left in 'publishing' by a run that died
mid-call is checked against the Page's recent posts once its lease expires,
never blindly re-posted."""

from datetime import timedelta

import pytest

from test_social_posting import P1, ONE, call, draft, env, meta_calls, run, schedule  # noqa: F401


def test_expired_publishing_lease_checks_recent_posts_before_reposting(env):  # noqa: F811
    draft(env, channels=["facebook"])
    schedule(env, now=True)
    real = env.graph.publish_page_photo

    def dies_after_meta_created_the_post(token, page_id, image_url, caption):
        real(token, page_id, image_url, caption)
        env.graph.posts[-1]["created_time"] = "2026-11-04T12:00:03+0000"
        raise RuntimeError("Lambda timed out before saving the result")

    env.graph.publish_page_photo = dies_after_meta_created_the_post
    with pytest.raises(RuntimeError):
        run(env)
    assert call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]["state"] == "publishing"
    env.graph.publish_page_photo = real

    env.clock.value += timedelta(minutes=6)  # the lease has expired; an async retry arrives
    assert run(env) == {"result": "published"}
    fb = call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]
    assert fb["metaPostId"] == env.graph.posts[0]["id"]
    assert len(env.graph.posts) == 1
    assert len(meta_calls(env, "recent_page_posts")) == 1
    assert len(meta_calls(env, "publish_page_photo")) == 1  # only the first, interrupted call


def test_expired_publishing_lease_with_no_post_on_the_page_publishes(env):  # noqa: F811
    draft(env, channels=["facebook"])
    schedule(env, now=True)
    real = env.graph.publish_page_photo

    def dies_before_meta(*args):
        raise RuntimeError("Lambda died before Meta answered")

    env.graph.publish_page_photo = dies_before_meta
    with pytest.raises(RuntimeError):
        run(env)
    env.graph.publish_page_photo = real
    env.clock.value += timedelta(minutes=6)
    assert run(env) == {"result": "published"}
    assert len(meta_calls(env, "recent_page_posts")) == 1
    assert len(env.graph.posts) == 1
