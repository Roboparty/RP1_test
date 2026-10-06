FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY sql ./sql
COPY imports ./imports
COPY tests ./tests
COPY scripts ./scripts
COPY seeds ./seeds

CMD ["alembic", "upgrade", "head"]
