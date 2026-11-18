from shared.features.phone_agent import offers as of


def test_free_requested_time_is_said_first_and_plainly():
    answer = of.requested_first({"times": ["16:30", "17:00", "17:30"], "before": ["16:30"], "after": ["17:30"],
                                 "nearby": [], "dayFull": False, "requestedTime": "17:00"}, "17:00")
    assert list(answer)[:2] == ["requestedTime", "requestedTimeFree"]
    assert answer["requestedTimeFree"] is True


def test_taken_requested_time_is_false():
    answer = of.requested_first({"times": ["16:30", "17:30"], "before": [], "after": [], "nearby": [],
                                 "dayFull": False}, "17:00")
    assert answer["requestedTimeFree"] is False and answer["requestedTime"] == "17:00"


def test_no_time_asked_changes_nothing():
    answer = {"times": ["17:00"], "before": [], "after": [], "nearby": [], "dayFull": False}
    assert of.requested_first(answer, None) is answer


def test_prompt_tells_the_model_to_trust_the_verdict():
    from shared.features.phone_agent import copy
    assert "requestedTimeFree" in copy.__dict__.get("SYSTEM_PROMPT", "") or any(
        "requestedTimeFree" in v for v in copy.__dict__.values() if isinstance(v, str))
