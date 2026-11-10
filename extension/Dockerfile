# syntax=docker/dockerfile:1
# Builds the unpacked Chrome extension. `docker compose up` runs this image once: it copies the build to
# extension/dist on your machine, which you load with "Load unpacked" on chrome://extensions.

FROM node:22-slim

WORKDIR /src
COPY . .
RUN npm ci --no-audit --no-fund && npm run build

# /host is the extension/ folder of your checkout (see ../compose.yaml). The files are handed to the folder's owner,
# so on Linux they aren't left owned by root.
CMD ["sh", "-c", "rm -rf /host/dist && cp -r .output/chrome-mv3 /host/dist && (chown -R \"$(stat -c %u:%g /host)\" /host/dist 2>/dev/null || true) && echo 'ClickyTabs extension ready in extension/dist: load it with Load unpacked on chrome://extensions'"]
