FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY app ./app
COPY knowledge ./knowledge
# Required for partner deployments (git-ignored; get it from the integration team).
COPY internal_terms.txt ./

RUN useradd --create-home appuser && mkdir -p /srv/logs && chown appuser /srv/logs
USER appuser

EXPOSE 8000
# PORT is set by most hosts (Render, Railway); defaults to 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
