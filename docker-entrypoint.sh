#!/bin/sh
set -e

# Starts as root (see Dockerfile's note on why there's no `USER` directive)
# so it can fix ownership of the bind-mounted instance/stores/data
# directories, then drops to the unprivileged `appuser` for everything
# else — gunicorn itself never runs as root.
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"

# Keep appuser's UID/GID in sync with what's actually requested at
# runtime — the image bakes in APP_UID/APP_GID as build args (so a fresh
# build already matches your deploy user), but if .env's values change
# without a rebuild, this still gets it right without one.
current_uid="$(id -u appuser)"
current_gid="$(id -g appuser)"
if [ "$current_gid" != "$APP_GID" ]; then
    groupmod -o -g "$APP_GID" appuser
fi
if [ "$current_uid" != "$APP_UID" ]; then
    usermod -o -u "$APP_UID" appuser
fi

# One-time (well — every start, but cheap and idempotent) fixup: these
# three are the only directories the app ever writes into (see
# docker-compose.yml's bind mounts), and anything already in them from
# before this change (or created by a previous image that ran as root)
# needs to become appuser's before appuser can write to it.
echo "Fixing ownership of instance/, stores/, data/ ..."
chown -R appuser:appuser /app/instance /app/stores /app/data

echo "Initializing database schema..."
gosu appuser python scripts/init_db.py

echo "Starting gunicorn (as appuser, uid=$(id -u appuser))..."
exec gosu appuser gunicorn --bind 0.0.0.0:8000 --workers 3 --timeout 60 \
    --worker-tmp-dir /dev/shm --access-logfile - --error-logfile - \
    wsgi:app
