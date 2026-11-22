#!/usr/bin/env bash
set -euo pipefail

umask 077

BACKUP_DIR="/home/ethan/backups/homelab"
STAGE="$(mktemp -d /tmp/homelab-backup.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p -m 700 "$BACKUP_DIR"
chown ethan:ethan "$BACKUP_DIR"

mkdir -p \
    "$STAGE/postgres" \
    "$STAGE/n8n" \
    "$STAGE/grafana" \
    "$STAGE/npm" \
    "$STAGE/configs" \
    "$STAGE/configs/systemd" \
    "$STAGE/carnet-force" \
    "$STAGE/monitoring" \
    "$STAGE/gitea" \
    "$STAGE/homepage" \
    "$STAGE/adguard" \
    "$STAGE/tts"

backup_sqlite() {
    local src_path="$1"
    local dst_path="$2"
    python3 - "$src_path" "$dst_path" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()
    src.close()
PY
}

# 1. PostgreSQL dumps (postgres-force)
docker exec postgres-force pg_dump -U ethan -Fc powertrack > "$STAGE/postgres/powertrack.dump"
docker exec postgres-force pg_dump -U ethan --no-owner powertrack > "$STAGE/postgres/powertrack.sql"
docker exec postgres-force pg_dump -U ethan -Fc db_entrainement > "$STAGE/postgres/db_entrainement.dump"

# 2. n8n SQLite DB, encryption config, and workflow JSON export
N8N_DATA="/var/lib/docker/volumes/n8n-docker_n8n_data/_data"
backup_sqlite "$N8N_DATA/database.sqlite" "$STAGE/n8n/database.sqlite"
if [ -f "$N8N_DATA/config" ]; then
    cp -a "$N8N_DATA/config" "$STAGE/n8n/config"
fi

if docker exec -u node n8n n8n export:workflow --all --output=/tmp/n8n-workflows-backup.json >/dev/null 2>&1; then
    docker cp n8n:/tmp/n8n-workflows-backup.json "$STAGE/n8n/workflows.json" || echo "WARN: failed to copy n8n workflows.json" >&2
    docker exec -u node n8n rm -f /tmp/n8n-workflows-backup.json || true
else
    echo "WARN: n8n export:workflow failed" >&2
fi

# 3. Grafana & Nginx Proxy Manager SQLite DBs
backup_sqlite "/var/lib/docker/volumes/monitoring_grafana_data/_data/grafana.db" "$STAGE/grafana/grafana.db"
if [ -f "/home/ethan/npm/data/database.sqlite" ]; then
    backup_sqlite "/home/ethan/npm/data/database.sqlite" "$STAGE/npm/database.sqlite"
fi

# 4. PKI & service configs
if [ -d "/home/ethan/homelab-ca" ]; then
    cp -a "/home/ethan/homelab-ca" "$STAGE/homelab-ca"
fi
if [ -d "/home/ethan/npm/data/custom_ssl" ]; then
    cp -a "/home/ethan/npm/data/custom_ssl" "$STAGE/npm/custom_ssl"
fi
if [ -f "/home/ethan/docker-compose.yml" ]; then
    cp -a "/home/ethan/docker-compose.yml" "$STAGE/configs/root-docker-compose.yml"
fi

for f in docker-compose.yml .env init-powertrack.sql; do
    if [ -f "/home/ethan/carnet-force/$f" ]; then
        cp -a "/home/ethan/carnet-force/$f" "$STAGE/carnet-force/$f"
    fi
done

if [ -f "/home/ethan/n8n-docker/docker-compose.yml" ]; then
    cp -a "/home/ethan/n8n-docker/docker-compose.yml" "$STAGE/n8n/docker-compose.yml"
fi

for f in docker-compose.yml prometheus.yml; do
    if [ -f "/home/ethan/monitoring/$f" ]; then
        cp -a "/home/ethan/monitoring/$f" "$STAGE/monitoring/$f"
    fi
done

if [ -f "/home/ethan/gitea-docker/docker-compose.yml" ]; then
    cp -a "/home/ethan/gitea-docker/docker-compose.yml" "$STAGE/gitea/docker-compose.yml"
fi

if [ -f "/home/ethan/homepage-docker/docker-compose.yml" ]; then
    cp -a "/home/ethan/homepage-docker/docker-compose.yml" "$STAGE/homepage/docker-compose.yml"
fi
if [ -f "/home/ethan/homepage-docker/.env" ]; then
    cp -a "/home/ethan/homepage-docker/.env" "$STAGE/homepage/.env"
fi
if [ -d "/home/ethan/homepage-docker/config" ]; then
    cp -a "/home/ethan/homepage-docker/config" "$STAGE/homepage/config"
fi

for tts_file in Dockerfile docker-compose.yml server.py; do
    if [ -f "/home/ethan/tts-docker/$tts_file" ]; then
        cp -a "/home/ethan/tts-docker/$tts_file" "$STAGE/tts/$tts_file"
    fi
done

if [ -f "/usr/local/sbin/homelab-health-alert.sh" ]; then
    cp -a "/usr/local/sbin/homelab-health-alert.sh" "$STAGE/configs/homelab-health-alert.sh"
fi
if [ -f "/etc/apt/apt.conf.d/51homelab-unattended" ]; then
    cp -a "/etc/apt/apt.conf.d/51homelab-unattended" "$STAGE/configs/51homelab-unattended"
fi
for unit in homelab-backup.service homelab-backup.timer homelab-backup-alert.service homelab-cert-check.service homelab-cert-check.timer; do
    if [ -f "/etc/systemd/system/$unit" ]; then
        cp -a "/etc/systemd/system/$unit" "$STAGE/configs/systemd/$unit"
    fi
done

if [ -f "/home/ethan/adguard/confdir/AdGuardHome.yaml" ]; then
    cp -a "/home/ethan/adguard/confdir/AdGuardHome.yaml" "$STAGE/adguard/AdGuardHome.yaml"
fi

# 5. Package, validate, install atomically, and prune backups older than 14 days
TS="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_TMP="$BACKUP_DIR/.homelab_backup_${TS}.tar.gz.tmp"
ARCHIVE_FINAL="$BACKUP_DIR/homelab_backup_${TS}.tar.gz"

tar -czf "$ARCHIVE_TMP" -C "$STAGE" .
tar -tzf "$ARCHIVE_TMP" > /dev/null
mv "$ARCHIVE_TMP" "$ARCHIVE_FINAL"
chown ethan:ethan "$ARCHIVE_FINAL"
chmod 600 "$ARCHIVE_FINAL"

ln -sfn "$ARCHIVE_FINAL" "$BACKUP_DIR/latest.tar.gz"
chown -h ethan:ethan "$BACKUP_DIR/latest.tar.gz"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'homelab_backup_*.tar.gz' -mtime +14 -delete

ls -lh "$ARCHIVE_FINAL"
