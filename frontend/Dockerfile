FROM node:22-alpine

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
RUN apk add --no-cache su-exec \
 && chown -R node:node /app \
 && chmod +x /app/docker-entrypoint.sh

EXPOSE 5173
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
