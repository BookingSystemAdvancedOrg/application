# voice-gateway

Stage 2 of the Booqy phone agent: a streaming voice front (VAD, Amazon Transcribe streaming, turn and
barge-in detection, rule based backchannels, Amazon Polly) around the unchanged phone agent in
`shared/features/phone_agent`. Design, ownership and interfaces: [DESIGN.md](DESIGN.md).

Status: skeleton. Interfaces are fixed; implementations are being built in parallel.

## Run the tests

From the application root:

```
python -m pytest services/voice-gateway -q            # contracts and pure logic (no extra deps)
pip install -r services/voice-gateway/requirements-dev.txt
python -m pytest services/voice-gateway -q            # everything with the runtime libraries
VG_HARNESS=fake python -m pytest services/voice-gateway/tests/test_vg_scenarios.py -q
AWS_PROFILE=booqy-dev VG_HARNESS=live python -m pytest services/voice-gateway/tests/test_vg_scenarios.py -q
```

The live harness calls Transcribe, Polly and Bedrock in the dev account (cents per run); tables and tool
Lambdas stay faked, so it writes no dev data.

## Run one local call

```
python -m voice_gateway.server local --caller caller.wav --out call.wav --fake
```

## Container

```
docker build -f services/voice-gateway/Dockerfile -t voice-gateway:dev .
```

Built from the application root (it copies `shared/` and `functions/phone-agent-call/app.py`).
Deployment (Fargate, IAM, the AWS phone adapter) is a later step: DESIGN.md section 10.
