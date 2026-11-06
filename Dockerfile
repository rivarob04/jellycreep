FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --uid 1000 --create-home jellycreep \
 && mkdir -p /data && chown jellycreep:jellycreep /data
USER jellycreep
VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4).status == 200 else 1)"

# No --proxy-headers on purpose: the trusted-proxy check must see the real peer
# (your reverse proxy / Authentik outpost), not a client-supplied X-Forwarded-For.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8484", "--no-server-header"]
