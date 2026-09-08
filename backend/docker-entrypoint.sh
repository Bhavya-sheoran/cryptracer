#!/bin/sh
# Drop to the unprivileged `app` user before exec'ing the server.
#
# Why this exists rather than a plain `USER app` in the Dockerfile: the
# evidence/reports store is a named Docker volume. Docker only seeds a volume's
# ownership from the image the first time it creates it, so a volume created
# while this image still ran as root stays root-owned - and the app would lose
# the ability to write the very evidence files it exists to preserve.
#
# So the container still *starts* as root, fixes ownership, and immediately
# drops. That is the same pattern the official postgres and nginx images use.
# If it is already running unprivileged (someone passed `user:` in compose),
# there is nothing to do but exec.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R app:app /app/storage 2>/dev/null || true
    # setpriv comes from util-linux, already in the debian slim base - no extra
    # package, and unlike `su` it does not fork a shell between us and the app,
    # so signals reach uvicorn directly and the container stops cleanly.
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
