#!/bin/sh
# Drop to the unprivileged `node` user before starting Vite.
#
# node_modules is an anonymous volume in compose, and Docker seeds a volume's
# ownership from the image only when it first creates it - a volume made while
# this image ran as root stays root-owned, and Vite would then fail to write
# its dependency cache. So fix ownership as root, then drop immediately.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R node:node /app/node_modules 2>/dev/null || true
    exec su-exec node "$@"
fi

exec "$@"
