FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

WORKDIR /app
COPY src/ ./src/
COPY run.sh /run.sh
RUN chmod a+x /run.sh
RUN useradd -m -s /bin/bash cycling && chown -R cycling:cycling /app

USER cycling

CMD [ "/run.sh" ]