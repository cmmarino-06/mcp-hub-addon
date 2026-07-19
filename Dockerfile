FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY run.sh /run.sh
RUN chmod +x /run.sh

# NOTE: no `USER mcp` here. Container starts as root so run.sh can read
# /data/options.json (not world-readable under Supervisor), then execs
# the actual server as the unprivileged `mcp` user via gosu.
ENTRYPOINT ["/run.sh"]
