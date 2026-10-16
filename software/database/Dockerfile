FROM python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY sql ./sql
COPY imports ./imports
COPY tests ./tests

CMD ["alembic", "upgrade", "head"]
