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
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libharfbuzz0b libffi8 shared-mime-info && \
    rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl duckduckgo-search && rm -rf /wheels
RUN mkdir -p /var/walacor/wal && chown app:app /var/walacor/wal
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=2 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
ENTRYPOINT ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
