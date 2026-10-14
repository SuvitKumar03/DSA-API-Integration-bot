FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY app ./app
COPY knowledge ./knowledge
# Private files are NOT baked into the image:
#   knowledge/*.md      -> Cloud Storage bucket (KNOWLEDGE_BACKEND=gcs), or mount knowledge/ locally
#   internal_terms.txt  -> Secret Manager file mount (INTERNAL_TERMS_FILE), or mount it locally:
#   docker run -p 8000:8000 --env-file .env -v "$PWD/knowledge:/srv/knowledge" \
#     -v "$PWD/internal_terms.txt:/srv/internal_terms.txt:ro" integration-bot

RUN useradd --create-home appuser && mkdir -p /srv/logs && chown appuser /srv/logs
USER appuser

EXPOSE 8000
# PORT is set by the host (Cloud Run sets 8080); defaults to 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
