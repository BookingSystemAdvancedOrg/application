# Booqy voice gateway. Built from the APPLICATION REPO ROOT (it copies shared/ and the phone-agent-call module):
#   docker build -f services/voice-gateway/Dockerfile -t voice-gateway:dev .
# Target: ECS Fargate ARM64 (DESIGN.md 10); builds on amd64 too. No secrets in the image: AWS access comes from
# the task role at run time. Every runtime wheel exists for linux/arm64 and Python 3.13 (checked 2026-11-20).

# --- stage 1: the silero VAD model (MIT), pinned by version and sha256 --------------------------------------------
# Taken from the silero-vad wheel on PyPI without its dependencies (no torch): the wheel is pure Python plus the
# model files, and only data/silero_vad.onnx is kept. The build fails when the file is not the pinned one.
FROM public.ecr.aws/docker/library/python:3.13-slim AS models
ARG SILERO_VAD_VERSION=6.2.3
ARG SILERO_VAD_SHA256=1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3
RUN pip download --no-deps --no-cache-dir --only-binary=:all: "silero-vad==${SILERO_VAD_VERSION}" -d /tmp/wheel \
    && mkdir -p /opt/models \
    && python -c "import glob, zipfile; z = zipfile.ZipFile(glob.glob('/tmp/wheel/*.whl')[0]); open('/opt/models/silero_vad.onnx', 'wb').write(z.read('silero_vad/data/silero_vad.onnx'))" \
    && echo "${SILERO_VAD_SHA256}  /opt/models/silero_vad.onnx" | sha256sum -c - \
    && rm -rf /tmp/wheel

# --- stage 2: the gateway -------------------------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VG_APP_ROOT=/app \
    VG_SILERO_MODEL=/opt/models/silero_vad.onnx \
    PYTHONPATH=/app:/app/services/voice-gateway
WORKDIR /app

COPY services/voice-gateway/requirements.txt services/voice-gateway/requirements.txt
RUN pip install --no-cache-dir -r services/voice-gateway/requirements.txt

COPY --from=models /opt/models/silero_vad.onnx /opt/models/silero_vad.onnx

# The phone agent itself is reused, never copied or rewritten: shared/ and the phone-agent-call module.
COPY shared/ shared/
COPY functions/phone-agent-call/app.py functions/phone-agent-call/app.py
COPY services/voice-gateway/voice_gateway/ services/voice-gateway/voice_gateway/

# Non-root, no home, no shell login; the image is read only for it (the clip cache lives in memory).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway
USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "voice_gateway.server", "serve", "--port", "8080"]
