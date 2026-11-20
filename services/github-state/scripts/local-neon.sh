#!/usr/bin/env bash
# Start a disposable PostgreSQL 18 behind Neon's own proxy (HTTP and WebSocket), so
# `npm run test:postgres` runs the production driver unchanged with no Neon account. The proxy
# answers on 443 for a *.neon.tech test hostname mapped to 127.0.0.1 in /etc/hosts, with a
# self-signed certificate that Node is told to trust. Needs Docker, openssl and sudo (CI runners).
# Prints the environment to export; GitHub Actions callers append it to $GITHUB_ENV.
set -euo pipefail
dir=${1:?usage: local-neon.sh CERTIFICATE_DIRECTORY}
domain=us-east-2.aws.neon.tech
endpoint=ep-local.$domain
# Neon proxy built for local PostgreSQL authentication (github.com/TimoWilhelm/local-neon-http-proxy).
proxy_image=ghcr.io/timowilhelm/local-neon-http-proxy@sha256:cd2ae14edf2feafbc3330492de5c80506f77274c3bd013154cdef697bdeb768a
mkdir -p "$dir"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=*.$domain" -addext "subjectAltName=DNS:*.$domain" \
  -keyout "$dir/proxy.key" -out "$dir/proxy.pem" 2>/dev/null
docker network create neon-local >/dev/null
docker run -d --name neon-local-postgres --network neon-local -e POSTGRES_PASSWORD=postgres postgres:18 \
  -c max_connections=500 >/dev/null
# TCP readiness: the image's first-start initialisation server listens only on its socket.
until docker exec neon-local-postgres pg_isready -h 127.0.0.1 -U postgres -q; do sleep 1; done
docker exec -i neon-local-postgres psql -q -U postgres -v ON_ERROR_STOP=1 < "$(dirname "$0")/local-neon.sql"
docker run -d --name neon-local-proxy --network neon-local -p 443:4445 -v "$dir:/certs:ro" --entrypoint ./neon-proxy \
  "$proxy_image" -c /certs/proxy.pem -k /certs/proxy.key --auth-backend=postgres \
  --auth-endpoint=postgres://postgres:postgres@neon-local-postgres:5432/postgres --wss=0.0.0.0:4445 >/dev/null
# The driver sends HTTP queries to api.<domain> and WebSocket sessions to the endpoint host.
echo "127.0.0.1 $endpoint api.$domain" | sudo tee -a /etc/hosts >/dev/null
url="postgresql://github_state_test_app:app@$endpoint/github_state_test?sslmode=require"
for attempt in $(seq 30); do
  curl -sf --cacert "$dir/proxy.pem" "https://api.$domain/sql" -H "Neon-Connection-String: $url" \
    -d '{"query":"SELECT 1","params":[]}' >/dev/null && break
  if [ "$attempt" = 30 ]; then docker logs neon-local-proxy | tail -20; exit 1; fi
  sleep 1
done
echo "TEST_POSTGRES_URL=$url"
echo "TEST_POSTGRES_MIGRATION_URL=postgresql://github_state_test_migration:migration@$endpoint/github_state_test?sslmode=require"
echo "NODE_EXTRA_CA_CERTS=$dir/proxy.pem"
