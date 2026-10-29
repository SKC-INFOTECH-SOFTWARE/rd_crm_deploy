#!/usr/bin/env sh
# Guarded application update for the internal Docker deployment.
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

for command_name in docker git curl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: $command_name" >&2
    exit 2
  }
done

test -f .env || { echo "Missing .env" >&2; exit 2; }
for file_name in import_config.json client_data_overrides.json \
  client_identity_groups.json ignored_clients.json client_profile_enrichment.json \
  supplier_directory.json; do
  test -f "runtime-config/$file_name" || {
    echo "Missing runtime-config/$file_name" >&2
    exit 2
  }
done

release=$(tr -d '[:space:]' < VERSION)
configured_release=$(sed -n 's/^RD_RELEASE=//p' .env | tail -n 1 | tr -d '\r')
test "$release" = "$configured_release" || {
  echo "VERSION ($release) and .env RD_RELEASE ($configured_release) differ" >&2
  exit 2
}

revision=$(git rev-parse --short=12 HEAD)
configured_revision=$(sed -n 's/^RD_REVISION=//p' .env | tail -n 1 | tr -d '\r')
test "$revision" = "$configured_revision" || {
  echo "Git revision ($revision) and .env RD_REVISION ($configured_revision) differ" >&2
  exit 2
}

docker compose config --quiet
docker compose build --pull
docker compose up -d database

# Never migrate until a verified backup of the current database exists.
docker compose run --rm --no-deps backup \
  python -m rd_ops backup --retention-days 30
docker compose run --rm migrate
docker compose run --rm --no-deps migrate \
  python -m rd_ops preflight --production

docker compose up -d --remove-orphans

attempt=0
until docker compose exec -T web python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 12 ]; then
    echo "Release $release failed readiness checks. Containers were left running for diagnosis." >&2
    echo "Use the rollback procedure in OPERATIONS.md; do not restore the database merely for an application rollback." >&2
    exit 3
  fi
  sleep 5
done

docker compose ps
echo "Release $release ($revision) is ready."
