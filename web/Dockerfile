# syntax=docker/dockerfile:1

FROM node:22-alpine AS build
WORKDIR /app

# Vite inlines VITE_* values into the bundle at build time, so they must be set before
# `npm run build`. Every one is public by design. Never pass a secret as a build arg.
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_PUBLISHABLE_KEY
ARG VITE_API_BASE=/api
ARG VITE_USE_MOCKS=false
ARG VITE_MAX_UPLOAD_MB=10
ARG VITE_SENTRY_DSN=
ARG VITE_SENTRY_ENVIRONMENT=development
ARG VITE_RELEASE=

# The lockfile goes in first, so the npm ci layer is reused until dependencies change.
# Don't set NODE_ENV=production before npm ci: it would skip the devDependencies the build needs.
COPY package.json package-lock.json .npmrc ./
RUN npm ci

COPY . .
RUN npm run build


FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html

# nginx.conf runs without root. The master creates its client and proxy temp directories
# under /var/cache/nginx, which the image leaves root-owned. Only the top directory needs
# the chown: nginx creates the subdirectories itself. The pid file goes to /tmp, and the
# server listens on 8080 because a non-root process cannot bind port 80.
RUN chown nginx:nginx /var/cache/nginx
USER nginx
EXPOSE 8080
