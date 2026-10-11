# syntax=docker/dockerfile:1
# imgagent — portable CPU-only container. No browser required.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && pip cache purge

# Default writable state; override for persistent volumes:
#   docker run -v imgagent-data:/data -e IMGAGENT_WORKSPACE=/data/workspace \
#     -e IMGAGENT_ASSETS_ROOT=/data/assets imgagent run "query" --json
ENV IMGAGENT_WORKSPACE=/data/workspace \
    IMGAGENT_ASSETS_ROOT=/data/assets
VOLUME ["/data"]

RUN useradd --create-home agent && chown -R agent:agent /data
USER agent

ENTRYPOINT ["imgagent"]
CMD ["--help"]
