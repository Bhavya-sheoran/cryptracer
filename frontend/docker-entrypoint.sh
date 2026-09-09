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
    # setpriv, from util-linux in the Debian base. The Alpine image used
    # su-exec, which Debian does not ship; setpriv is equivalent and, unlike
    # `su`, does not put a shell between us and Vite - so signals reach the
    # dev server and the container stops cleanly.
    exec setpriv --reuid=node --regid=node --init-groups "$@"
fi

exec "$@"
