# Debian (glibc), not Alpine (musl), and the reason is the lockfile.
#
# Rollup ships its native binary as a set of platform-specific optional
# dependencies. npm records whichever matches the machine that generated the
# lockfile and then, through a long-standing bug (npm/cli#4828), `npm ci` on a
# different platform fails to install the one it actually needs. A lockfile
# built on Alpine names @rollup/rollup-linux-x64-musl; CI runs on Ubuntu and
# needs -gnu, so vitest died at startup with "Cannot find module".
#
# Pinning both to glibc removes the mismatch rather than working around npm.
FROM node:26-slim

WORKDIR /app

# `npm ci` installs exactly what package-lock.json pins, so an image built
# today and one built in six months contain the same dependency tree. `npm
# install` would silently take newer minors and make a build unreproducible -
# which for a forensic tool is a provenance problem, not just a hygiene one.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# The node images ship an unprivileged `node` user (uid 1000). Vite writes its
# dependency cache into node_modules/.vite, which compose mounts as an
# anonymous volume, so ownership is fixed at start rather than build time - the
# volume may pre-date this change and still be root-owned.
#
# Note this image runs the Vite *dev server*. It is a development artifact and
# should never be the production frontend: build the static bundle and serve it
# from a web server instead.
# setpriv comes from util-linux in the Debian base, so no package to install -
# the Alpine build needed su-exec, which does not exist here.
RUN chown -R node:node /app \
 && chmod +x /app/docker-entrypoint.sh

EXPOSE 5173
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
