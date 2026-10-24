FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY fixtures ./fixtures
COPY scripts/entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh && chown -R appuser /srv
USER appuser

ENV PORT=8000 DATABASE_URL=sqlite:////srv/data/claims.db INGEST_ON_START=1
RUN mkdir -p /srv/data
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
ENTRYPOINT ["./entrypoint.sh"]
