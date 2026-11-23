# Coconut on a Linux host (docs/cloud.md): the service with Claude's Agent SDK on Bedrock, the Linux end of the relay
# tunnel, and Litestream, which streams the database to S3 and restores it on a new host.
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*

ARG LITESTREAM=0.5.18
RUN arch=$(dpkg --print-architecture | sed 's/amd64/x86_64/') \
    && curl -fsSL -o /tmp/litestream.tar.gz \
       "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM}/litestream-${LITESTREAM}-linux-${arch}.tar.gz" \
    && tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream && rm /tmp/litestream.tar.gz \
    && litestream version

# The SDK wheel carries the Claude Code binary for this platform.
RUN pip install --no-cache-dir claude-agent-sdk==0.2.187 cryptography websockets

COPY backend /app/backend
COPY cloud /app/cloud
ENV PYTHONPATH=/app/backend PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME /data
ENTRYPOINT ["/app/cloud/entrypoint.sh"]
