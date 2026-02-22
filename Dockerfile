ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --break-system-packages .

COPY run.sh /etc/services.d/shutter_control/run
RUN chmod a+x /etc/services.d/shutter_control/run
