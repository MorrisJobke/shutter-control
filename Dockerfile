FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

RUN mkdir -p /data
COPY config.yaml /data/config.yaml
ENV CONFIG_PATH=/data/config.yaml

CMD ["sh", "-c", "shutter-control \"$CONFIG_PATH\""]
