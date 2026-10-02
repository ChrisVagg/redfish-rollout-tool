# The exporter: poller.py exporter with requirements.txt. The repo itself is mounted at /app (docker-compose.yaml).
FROM python:3.11-slim
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /app
ENTRYPOINT ["python", "poller.py", "exporter"]
