FROM nginx:1.27-alpine
COPY gateway/nginx.conf /etc/nginx/nginx.conf
EXPOSE 80
# Use 127.0.0.1, not localhost: nginx listens on IPv4 :80, but "localhost" can
# resolve to IPv6 ::1 first -> "connection refused" -> false-unhealthy.
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1/gateway/health || exit 1
