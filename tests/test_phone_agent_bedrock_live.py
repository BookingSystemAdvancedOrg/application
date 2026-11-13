"""The scripted calls against real Bedrock (ADR-L test plan: "a marked
integration test runs the same scripts against real Bedrock"). Skipped
unless AGENT_MODEL_LIVE_TEST=<model id> and AWS credentials for an account
with access to that model are present; never runs in CI.

    AWS_PROFILE=booqy-dev AGENT_MODEL_LIVE_TEST=eu.anthropic.claude-haiku-4-5-20251001-v1:0 \\
        PYTHONPATH=.:tests python -m pytest -s tests/test_phone_agent_bedrock_live.py
"""

import os

import pytest

from shared.agent_model_real import RealAgentModel
from test_phone_agent_calls import Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import env  # noqa: F401 (fixture)

MODEL = os.environ.get("AGENT_MODEL_LIVE_TEST")
pytestmark = pytest.mark.skipif(not MODEL, reason="set AGENT_MODEL_LIVE_TEST to a Bedrock model id")
TOMORROW = "2026-11-03"
SCRIPTS = {
    "sv": ["Hej, jag vill boka ett bord för fyra imorgon klockan sju på kvällen.", "Halv åtta passar bra.",
           "Ja, det stämmer.", "Anna Svensson.", "Ja, boka.", "Ja."],
    "en": ["Hi, I'd like to book a table for four tomorrow at seven in the evening.", "Half past seven works.",
           "Yes, that's right.", "Anna Svensson.", "Yes, please book it.", "Yes."],
}


def _credentials():
    import boto3
    frozen = boto3.Session(profile_name=os.environ.get("AWS_PROFILE")).get_credentials().get_frozen_credentials()
    return {"aws_access_key_id": frozen.access_key, "aws_secret_access_key": frozen.secret_key,
            "aws_session_token": frozen.token}


CREDENTIALS = _credentials() if MODEL else None   # read before moto patches botocore


def converse_outside_moto(credentials, model_id, system, messages, tools, max_tokens):
    """Runs in a fresh process (moto patches botocore in this one)."""
    import boto3
    model = RealAgentModel(model_id=model_id)
    model._client = boto3.client("bedrock-runtime", region_name="eu-north-1", **credentials)
    return model.converse(system, messages, tools, max_tokens=max_tokens)


class OutOfProcessModel:
    def __init__(self):
        import concurrent.futures
        import multiprocessing
        self.pool = concurrent.futures.ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))

    def converse(self, system, messages, tools, *, max_tokens):
        return self.pool.submit(converse_outside_moto, CREDENTIALS, MODEL, system, messages, tools,
                                max_tokens).result()


@pytest.fixture
def real(live):
    model = OutOfProcessModel()
    live.live._clients["model"] = model
    yield live
    model.pool.shutdown()


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_a_booking_call_with_a_counter_offer_on_real_bedrock(real, lang):
    real.invoke.layout = None
    real.availability.set(TOMORROW, ["17:30", "18:00", "19:30", "21:00"])   # 19:00 is taken
    phone = Phone(real)
    print(f"\n--- {MODEL} ({lang}) ---\nAGENT: {phone.turns()[0]['text']}")
    for text in SCRIPTS[lang]:
        reply = phone.say(text)
        print(f"CALLER: {text}\nAGENT: {reply}")
        if real.invoke.reservations:
            break
    booking = next(iter(real.invoke.reservations.values()), None)
    assert booking, "no booking was made"
    assert booking["date"] == TOMORROW and booking["startTime"] == "19:30" and booking["partySize"] == 4
    assert phone.row()["counterOffer"] == "other_time"


def test_a_caller_in_turkish_on_real_bedrock(real):
    phone = Phone(real)
    reply = phone.say("Merhaba, yarın akşam saat yedide dört kişilik bir masa ayırtmak istiyorum.")
    print(f"\n--- {MODEL} (tr) ---\nAGENT: {reply}")
    assert phone.row()["language"] == "tr"
    assert reply.startswith("Location loc-a-1 için bir yapay zekâ asistanıyım.")
