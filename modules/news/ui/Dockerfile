# ============================================================
# Sentinel Codex — News Radar config page (module "news")
# ============================================================
# Small Flask app (gunicorn) behind Cyber Sentinel's nginx-proxy. Talks to
# database knowledge_hub as the module's editor role created by
# modules/news/db/10_news.sql — never as postgres / the n8n role.
# Built and started by docker/docker-compose-sentinel-codex.yml (playbook 04).
# ============================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py ./
COPY templates/ templates/
COPY static/ static/

# Unprivileged runtime user.
RUN useradd --system --uid 10002 --no-create-home --shell /usr/sbin/nologin newsui
USER 10002

EXPOSE 8000

# Shallow check only — DB outages show up in the UI, they must not make
# Docker restart-loop the container.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# One worker: the login throttle is in-process.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", "--forwarded-allow-ips", "*", "app:app"]
