FROM ghcr.io/home-assistant/amd64-base-python:3.12

# Set Python environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Copy application source
WORKDIR /app
COPY src/ ./src/

# Copy entrypoint script
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
  io.hass.version="1.0.0" \
  io.hass.type="addon" \
  io.hass.arch="amd64"

CMD [ "/run.sh" ]