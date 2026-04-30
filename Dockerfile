FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DEEP_THINK_TRANSPORT=streamable-http
ENV DEEP_THINK_HOST=0.0.0.0
ENV DEEP_THINK_PORT=8002
ENV DEEP_THINK_DB=/data/jobs.db

VOLUME ["/data"]
EXPOSE 8002

CMD ["python", "-m", "deep_think_mcp"]
