FROM python:3.12-slim

# UID/GID the app actually runs as (see docker-entrypoint.sh) — defaults to
# 1000, the UID/GID of the first non-root user on a fresh Ubuntu/Debian
# install. Override at build
# time with --build-arg if your deploy user has different IDs (check with
# `id -u`/`id -g` as that user) — docker-compose.yml passes these through
# from .env's APP_UID/APP_GID automatically.
#
# Without this, every file the app creates in the bind-mounted instance/,
# stores/, data/ directories (SQLite db, store YAMLs, runtime CSVs)
# is owned by root on the HOST (containers run as root by default), so the
# deploy user can't edit or delete them without sudo.
ARG APP_UID=1000
ARG APP_GID=1000

# gosu: drops root privileges cleanly after the entrypoint's one-time
# ownership fixup (see docker-entrypoint.sh) — root starts the container
# (so it CAN chown the bind mounts) but gunicorn itself never runs as root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g "${APP_GID}" appuser \
    && useradd -u "${APP_UID}" -g appuser -m -s /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# docker-entrypoint.sh runs scripts/init_db.py (creates the DB schema, once,
# in its own process) BEFORE gunicorn starts — see that script's docstring
# for why: with gunicorn's default (no --preload) fork model, workers each
# import wsgi:app independently, and on a fresh database they'd all race
# db.create_all() against each other, crashing all but one with "table X
# already exists". Running it here first means the schema already exists by
# the time any worker forks. It also fixes ownership of instance/stores/data
# and drops from root to appuser (see that file) before either step runs.
#
# --workers 3: sensible default for a small VPS (2 vCPU) running mostly
#   I/O-bound work (waiting on the Anthropic/Shopify APIs)
#   for how to size this to your actual server.
# --timeout 60: a tool-use turn can chain a few Anthropic round-trips; gunicorn's
#   default 30s can kill a worker mid-request under normal (if slow) conditions.
# --worker-tmp-dir /dev/shm: avoids gunicorn's worker heartbeat file hitting
#   slow/small container disk — write it to tmpfs instead.
# --access-logfile/--error-logfile -: log to stdout/stderr so `docker compose
#   logs` captures them (see docker-compose.yml for log rotation).
#
# No USER directive here on purpose — the entrypoint needs to START as root
# (to chown the bind mounts, which may be root-owned from before this
# change, or simply owned by whichever UID created them on the host) and
# drops to appuser itself via gosu partway through. A static `USER appuser`
# here would make that chown fail with permission denied.
ENTRYPOINT ["./docker-entrypoint.sh"]
