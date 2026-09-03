FROM node:22-alpine

WORKDIR /app

# `npm ci` installs exactly what package-lock.json pins, so an image built
# today and one built in six months contain the same dependency tree. `npm
# install` would silently take newer minors and make a build unreproducible -
# which for a forensic tool is a provenance problem, not just a hygiene one.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
