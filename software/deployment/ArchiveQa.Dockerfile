FROM python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY service/requirements.txt service/requirements-dev.txt service/requirements.lock /tmp/
COPY database/requirements.txt /tmp/database-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-dev.txt -r /tmp/database-requirements.txt -c /tmp/requirements.lock jsonschema==4.26.0
WORKDIR /app
