FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DASHBOARD_DATA_DIR=/data \
    PORT=8000

WORKDIR /app
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data

# Exact tested versions. (requirements.txt is for local development.)
COPY requirements.lock.txt .
RUN pip install -r requirements.lock.txt

COPY --chown=app:app . .
USER app
EXPOSE 8000

# /health checks that the app is up and the database answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','8000'), timeout=4)" || exit 1

# One process per container: the in-process parts (per-user running-question cap, scan queue,
# request coalescing, sign-in attempt limiter) are per process. Run more containers, not more workers.
CMD ["sh", "-c", "exec python -m uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
