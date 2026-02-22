ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --break-system-packages .

# s6-overlay v3 service definition
COPY run.sh /etc/s6-overlay/s6-rc.d/shutter_control/run
RUN chmod a+x /etc/s6-overlay/s6-rc.d/shutter_control/run \
 && echo "longrun" > /etc/s6-overlay/s6-rc.d/shutter_control/type \
 && mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d \
 && touch /etc/s6-overlay/s6-rc.d/user/contents.d/shutter_control
