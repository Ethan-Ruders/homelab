#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/root/.homelab-alert.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE" >&2
    exit 1
fi
# shellcheck source=/dev/null
. "$ENV_FILE"

send_alert() {
    local severity="$1"
    local title="$2"
    local details="$3"

    python3 - "$severity" "$title" "$details" <<'PY' | curl -fsS -m 15 \
        --cacert /home/ethan/homelab-ca/homelab-root-ca.crt \
        --resolve n8n.homelab:443:127.0.0.1 \
        -H "Content-Type: application/json" \
        -H "X-API-Key: ${ALERT_API_KEY}" \
        --data-binary @- \
        "${ALERT_WEBHOOK_URL}" > /dev/null
import json, sys
print(json.dumps({
    "severity": sys.argv[1],
    "title": sys.argv[2],
    "details": sys.argv[3],
}))
PY
}

case "${1:-}" in
    --test)
        send_alert "ok" \
            "Homelab — Système d'alertes Discord opérationnel" \
            "Webhook homelab-alert, alerte d'échec de sauvegarde et contrôle quotidien (backup + TLS + disque) actifs sur Serveur-Linux."
        ;;
    --backup-failed)
        logs="$(journalctl -u homelab-backup.service -n 10 --no-pager 2>&1 || true)"
        send_alert "critical" \
            "Échec de la sauvegarde quotidienne Homelab" \
            "Service homelab-backup.service en erreur. Dernières lignes :"$'\n'"$logs"
        ;;
    --daily-check)
        issues=()
        severity="warning"
        LATEST="/home/ethan/backups/homelab/latest.tar.gz"
        CERT="/home/ethan/homelab-ca/wildcard.homelab.crt"

        if [ ! -e "$LATEST" ]; then
            severity="critical"
            issues+=("Archive latest.tar.gz absente dans /home/ethan/backups/homelab/.")
        else
            age_sec="$(python3 -c 'import os, time, sys; p = os.path.realpath(sys.argv[1]); print(int(time.time() - os.path.getmtime(p)))' "$LATEST")"
            if [ "$age_sec" -gt 93600 ]; then
                severity="critical"
                hours="$((age_sec / 3600))"
                issues+=("Dernière sauvegarde trop ancienne : ${hours}h (seuil 26h).")
            fi
        fi

        if [ ! -f "$CERT" ]; then
            issues+=("Certificat TLS introuvable : $CERT.")
        elif ! openssl x509 -checkend 2592000 -noout -in "$CERT" >/dev/null 2>&1; then
            end_date="$(openssl x509 -enddate -noout -in "$CERT" 2>/dev/null | cut -d= -f2 || echo 'inconnue')"
            issues+=("Certificat wildcard.homelab.crt expire dans moins de 30 jours (expiration : ${end_date}).")
        fi

        disk_pct="$(df --output=pcent / | tail -n 1 | tr -dc '0-9')"
        if [ -n "$disk_pct" ] && [ "$disk_pct" -ge 85 ]; then
            severity="critical"
            issues+=("Disque / occupé à ${disk_pct}% (seuil 85%).")
        fi

        if [ "${#issues[@]}" -gt 0 ]; then
            details="$(printf '• %s\n' "${issues[@]}")"
            send_alert "$severity" "Alerte santé Serveur-Linux (contrôle quotidien)" "$details"
        fi
        ;;
    *)
        echo "Usage: $0 {--test|--backup-failed|--daily-check}" >&2
        exit 2
        ;;
esac
