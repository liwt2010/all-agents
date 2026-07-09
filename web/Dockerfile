# Web UI build stage
FROM node:20-alpine AS build
WORKDIR /app

# Copy package files and install deps
COPY package.json package-lock.json* ./
RUN npm install

# Build the production bundle
COPY . .
RUN npm run build

# Serve stage
FROM nginx:1.27-alpine
WORKDIR /app
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget --spider -q http://localhost/api/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
