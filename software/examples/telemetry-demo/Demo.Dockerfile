FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY test-runner /opt/test-runner
COPY demo-client /opt/demo-client
RUN pip install --no-cache-dir /opt/test-runner /opt/demo-client
WORKDIR /runtime
ENTRYPOINT []
CMD ["python", "-m", "rp1_demo", "--help"]
