# Built from the APPLICATION REPO ROOT (it copies shared/ and the phone-agent-call module):
#   docker build -f services/voice-gateway/Dockerfile -t voice-gateway:dev .
# Fargate ARM64 (DESIGN.md 10). No secrets in the image; AWS access comes from the task role.
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VG_APP_ROOT=/app PYTHONPATH=/app:/app/services/voice-gateway
WORKDIR /app

COPY services/voice-gateway/requirements.txt services/voice-gateway/requirements.txt
RUN pip install --no-cache-dir -r services/voice-gateway/requirements.txt

# Silero VAD model (MIT). Builder 'listen' pins the release URL and its sha256 here; the build fails
# on a mismatch. Until then the gateway falls back to the energy VAD (log event vad_fallback).
# ADD --checksum=sha256:<pinned> https://github.com/snakers4/silero-vad/raw/<tag>/src/silero_vad/data/silero_vad.onnx /opt/models/silero_vad.onnx

COPY shared/ shared/
COPY functions/phone-agent-call/app.py functions/phone-agent-call/app.py
COPY services/voice-gateway/voice_gateway/ services/voice-gateway/voice_gateway/

RUN useradd --system --uid 10001 gateway
USER gateway
EXPOSE 8080
CMD ["python", "-m", "voice_gateway.server", "serve"]
