FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    GOPAY_LINK_DATA_DIR=/data \
    UPI_STRIPE_TOKEN_CACHE_DIR=/data/stripe-bundles

WORKDIR /app

COPY gopay-link/requirements.txt /tmp/requirements.txt

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && useradd --system --uid 10001 --create-home app \
    && mkdir -p /data/stripe-bundles \
    && chown -R app:app /data

COPY --chown=app:app payment_batch.py ./payment_batch.py
COPY --chown=app:app gopay/__init__.py gopay/gopay_extract.py ./gopay/
COPY --chown=app:app upi/stripe_token.py ./upi/
COPY --chown=app:app gopay-link/app.py ./gopay-link/app.py
COPY --chown=app:app gopay-link/static/ ./gopay-link/static/

USER app

EXPOSE 8791
VOLUME ["/data"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/healthz', timeout=2)"

CMD ["python", "gopay-link/app.py", "--host", "0.0.0.0", "--port", "8791"]
