# Build: docker build -f deploy/Dockerfile .
# Multi-stage: build wheel then minimal runtime
FROM python:3.12-slim AS builder
WORKDIR /build
COPY . /build/gateway
RUN pip install --no-cache-dir build && \
    pip wheel --no-deps -w /wheels ./gateway

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# Native libs for WeasyPrint (PDF compliance export). Without these the
# /v1/compliance/export?format=pdf endpoint returns 501 at runtime.
# `locales` + `en_US.UTF-8` are required by the intent ONNX model — its
# StringNormalizer op calls std::locale("en_US.UTF-8") on load; without
# the locale generated, the InferenceSession fails to initialize and
# IntentClassifier silently falls back to a 0.5-confidence default.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libharfbuzz0b libffi8 shared-mime-info \
        locales && \
    sed -i '/^# en_US.UTF-8/s/^# //' /etc/locale.gen && \
    locale-gen && \
    rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
RUN useradd --create-home --uid 1000 app
COPY --from=builder /wheels /wheels
# Compliance PDF (jinja2 + weasyprint) is bundled because the Pango/Cairo
# system libs installed above are useless without it — /v1/compliance/export
# returns 501 otherwise. duckduckgo-search powers the built-in web tool.
# cryptography is bundled because Ed25519 record signing (gated by
# WALACOR_RECORD_SIGNING_ENABLED) lives in src/gateway/crypto/signing.py —
# without it the gateway can't sign records even when the flag is on.
# boto3 backs `gateway.entrypoint` — it hydrates secrets from SSM at
# container start when WALACOR_SSM_PREFIX is set. Without it, secrets
# fall back to env / .env files (legacy path, still supported).
RUN pip install --no-cache-dir /wheels/*.whl duckduckgo-search 'jinja2>=3.1' 'weasyprint>=62.0' 'cryptography>=42.0' 'boto3>=1.34' && rm -rf /wheels
RUN mkdir -p /var/walacor/wal && chown app:app /var/walacor/wal
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=2 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
# `gateway.entrypoint` hydrates SSM-backed secrets (when WALACOR_SSM_PREFIX
# is set), then exec's uvicorn so PID 1 is the gateway and signals
# propagate cleanly. When WALACOR_SSM_PREFIX is unset, the entrypoint is
# a thin pass-through to uvicorn — preserves the legacy local-dev path.
ENTRYPOINT ["python", "-m", "gateway.entrypoint"]
