# TagTrail poller container.
#
# A small persistent runner: the FCM channel needs a process that stays alive
# long enough to receive async replies, so we run the APScheduler entrypoint.
#
# Build context = `poller/`.  Build from the repo root:
#   docker build -t tagtrail-poller -f poller/Dockerfile poller/

FROM python:3.11-slim AS base

# Why these system packages:
#   - git: pip installs GoogleFindMyTools direct from a pinned commit
#   - ca-certificates: TLS to Google APIs
#   - tini: clean PID 1 + signal forwarding (apscheduler SIGTERM handling)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Keep the runtime as a non-root user.
RUN useradd --create-home --shell /bin/bash --uid 10001 tagtrail
USER tagtrail
ENV PATH="/home/tagtrail/.local/bin:${PATH}"

WORKDIR /app

# Install poller deps first (cache-friendly).
COPY --chown=tagtrail:tagtrail pyproject.toml /app/pyproject.toml
COPY --chown=tagtrail:tagtrail __init__.py    /app/poller/__init__.py
RUN pip install --user --no-cache-dir /app

# Then copy the rest of the package (separate layer for fast iteration).
COPY --chown=tagtrail:tagtrail . /app/poller/

# No volume needed: the GFMT secrets bundle is shipped as $GFMT_SECRETS_JSON_B64
# and the poller writes it to <gfmt_dir>/Auth/secrets.json on first start if
# the file is missing. GFMT then uses that file as its own writable cache for
# the lifetime of the container.

# tini -> python module entrypoint runs the persistent scheduler.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "poller.scheduler"]
