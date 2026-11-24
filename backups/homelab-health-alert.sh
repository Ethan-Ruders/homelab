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

run_security_scan() {
    local dryrun="${1:-0}"
    local alerts_json
    alerts_json="$(python3 - "$dryrun" <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

dryrun = sys.argv[1] == "1"
loki_base = "http://127.0.0.1:3100"
state_path = "/var/lib/homelab-security-watch/state.json"
cooldown_sec = 1800
window_sec = 600

rules = [
    {
        "id": "ssh_auth_failures",
        "title": "Alerte sécurité : échecs SSH / sudo (10 min)",
        "stream": '{job="auth"} |~ "(?i)(Failed password|Invalid user|authentication failure|incorrect password|NOT in sudoers|Connection closed by authenticating user)"',
        "warn": 5,
        "crit": 15,
    },
    {
        "id": "npm_5xx",
        "title": "Alerte HTTP : pic d'erreurs 5xx sur NPM (10 min)",
        "stream": '{job="npm", log_type="access", status=~"5.."}',
        "warn": 10,
        "crit": None,
    },
    {
        "id": "npm_4xx_burst",
        "title": "Alerte sécurité : scan / rafale HTTP 401-403-404 sur NPM (10 min)",
        "stream": '{job="npm", log_type="access", status=~"401|403|404"}',
        "warn": 40,
        "crit": None,
    },
    {
        "id": "docker_fatal",
        "title": "Alerte conteneur : erreur fatale / OOM détectée (10 min)",
        "stream": '{job="docker"} |~ "(?i)(OOMKilled|fatal error:|panic:)"',
        "warn": None,
        "crit": 1,
    },
]

def loki_get(path, params):
    url = f"{loki_base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))

try:
    with urllib.request.urlopen(f"{loki_base}/ready", timeout=5) as resp:
        if resp.status != 200:
            print(f"WARN: Loki not ready (HTTP {resp.status}), skipping security scan.", file=sys.stderr)
            print("[]")
            sys.exit(0)
except Exception as exc:
    print(f"WARN: Loki unreachable ({exc}), skipping security scan.", file=sys.stderr)
    print("[]")
    sys.exit(0)

state = {}
if os.path.exists(state_path):
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

now = time.time()
now_ns = int(now * 1_000_000_000)
start_ns = int((now - window_sec) * 1_000_000_000)
alerts = []

for rule in rules:
    metric_query = f"sum(count_over_time({rule['stream']}[10m]))"
    try:
        res = loki_get("/loki/api/v1/query", {"query": metric_query, "time": str(now_ns)})
        results = res.get("data", {}).get("result", [])
        count = int(float(results[0]["value"][1])) if results else 0
    except Exception as exc:
        print(f"WARN: Loki query failed for {rule['id']}: {exc}", file=sys.stderr)
        continue

    severity = None
    if rule["crit"] is not None and count >= rule["crit"]:
        severity = "critical"
    elif rule["warn"] is not None and count >= rule["warn"]:
        severity = "warning"

    if dryrun:
        status = severity.upper() if severity else "OK"
        print(f"[DRY-RUN] {rule['id']}: count={count} status={status}", file=sys.stderr)
        continue

    if severity is None:
        continue

    last_sent = float(state.get(rule["id"], 0))
    if now - last_sent < cooldown_sec:
        continue

    samples = []
    try:
        qres = loki_get("/loki/api/v1/query_range", {
            "query": rule["stream"],
            "start": str(start_ns),
            "end": str(now_ns),
            "limit": "3",
            "direction": "BACKWARD",
        })
        for stream in qres.get("data", {}).get("result", []):
            container = stream.get("stream", {}).get("container")
            for _, line in stream.get("values", []):
                clean = " ".join(line.strip().split())
                if len(clean) > 160:
                    clean = clean[:157] + "..."
                prefix = f"[{container}] " if container else ""
                samples.append(f"• {prefix}{clean}")
                if len(samples) >= 3:
                    break
            if len(samples) >= 3:
                break
    except Exception:
        pass

    details = f"Occurrences sur 10 min : {count}"
    if samples:
        details += "\nExtraits récents :\n" + "\n".join(samples)

    alerts.append({
        "id": rule["id"],
        "severity": severity,
        "title": rule["title"],
        "details": details,
        "ts": now,
    })

print(json.dumps(alerts))
PY
)"

    if [ "$dryrun" = "1" ]; then
        return 0
    fi

    local sent_ids=()
    while IFS= read -r item_b64; do
        [ -z "$item_b64" ] && continue
        local parsed
        parsed="$(python3 -c 'import base64, json, sys; d=json.loads(base64.b64decode(sys.argv[1]).decode()); print(d["id"]); print(d["severity"]); print(d["title"]); print(base64.b64encode(d["details"].encode()).decode())' "$item_b64")"
        local rule_id severity title details_b64 details
        rule_id="$(printf '%s\n' "$parsed" | sed -n '1p')"
        severity="$(printf '%s\n' "$parsed" | sed -n '2p')"
        title="$(printf '%s\n' "$parsed" | sed -n '3p')"
        details_b64="$(printf '%s\n' "$parsed" | sed -n '4p')"
        details="$(python3 -c 'import base64, sys; print(base64.b64decode(sys.argv[1]).decode(), end="")' "$details_b64")"
        if send_alert "$severity" "$title" "$details"; then
            sent_ids+=("$rule_id")
        fi
    done < <(python3 -c 'import base64, json, sys
for item in json.loads(sys.argv[1]):
    print(base64.b64encode(json.dumps(item).encode()).decode())' "$alerts_json")

    if [ "${#sent_ids[@]}" -gt 0 ]; then
        mkdir -p -m 755 /var/lib/homelab-security-watch
        python3 - "${sent_ids[@]}" <<'PY'
import json, os, sys, time
path = "/var/lib/homelab-security-watch/state.json"
state = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
now = time.time()
for rid in sys.argv[1:]:
    state[rid] = now
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
os.replace(tmp, path)
PY
    fi
}

case "${1:-}" in
    --test)
        send_alert "ok" \
            "Homelab — Système d'alertes Discord opérationnel" \
            "Webhook homelab-alert, alerte d'échec de sauvegarde, Fail2ban, surveillance logs Loki et contrôle quotidien actifs sur Serveur-Linux."
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
    --fail2ban-ban)
        jail="${2:-sshd}"
        ip="${3:-inconnue}"
        failures="${4:-?}"
        send_alert "critical" \
            "Fail2ban : IP bannie (${jail})" \
            "IP : \`${ip}\`"$'\n'"Échecs détectés : ${failures} tentative(s)"$'\n'"Hôte : Serveur-Linux · Durée du ban : 1h"
        ;;
    --security-scan)
        run_security_scan 0
        ;;
    --security-scan-dryrun)
        run_security_scan 1
        ;;
    *)
        echo "Usage: $0 {--test|--backup-failed|--daily-check|--fail2ban-ban <jail> <ip> <failures>|--security-scan|--security-scan-dryrun}" >&2
        exit 2
        ;;
esac
