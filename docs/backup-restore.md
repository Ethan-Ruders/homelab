# Sauvegardes et Restauration

La résilience du homelab repose sur deux niveaux complémentaires :
1. **Niveau hyperviseur (hebdomadaire)** : snapshots complets des machines virtuelles sous Proxmox VE.
2. **Niveau applicatif sur `Serveur-Linux` (quotidien)** : export logique cohérent des bases de données, configurations et PKI.

---

## 1. Sauvegarde hebdomadaire Proxmox (`vzdump`)

Définie dans `/etc/pve/jobs.cfg` (miroir dans [`backups/proxmox/jobs.cfg`](../backups/proxmox/jobs.cfg)) :
- **Fréquence :** tous les dimanches à `04:00`
- **Cibles :** VM `100` (`OPNsense-FW`) et VM `101` (`Serveur-Linux`)
- **Mode & compression :** `snapshot`, compression `zstd`
- **Destination & rétention :** stockage Proxmox `local`, conservation des `2` derniers snapshots (`keep-last=2`)

---

## 2. Sauvegarde logique quotidienne (VM 101)

Orchestrée par `systemd` ([`homelab-backup.timer`](../backups/systemd/homelab-backup.timer) et [`homelab-backup.service`](../backups/systemd/homelab-backup.service)) exécutant [`homelab-backup.sh`](../backups/homelab-backup.sh) :

- **Planification :** tous les jours à **`03:30:00` UTC** (`RandomizedDelaySec=5m`, `Persistent=true` pour rattraper un éventuel arrêt).
- **Destination :** `/home/ethan/backups/homelab/homelab_backup_<YYYYMMDD_HHMMSS>.tar.gz` avec lien symbolique `latest.tar.gz`.
- **Sécurité des permissions :** `umask 077`, dossiers en `0700` et archives en `0600` (propriétaire `ethan:ethan`).
- **Validation & rétention :** création atomique via fichier temporaire, vérification immédiate d'intégrité (`tar -tzf`) avant mise à jour de `latest.tar.gz`, puis purge automatique au-delà de **14 jours** (`-mtime +14 -delete`).

### Contenu de chaque archive quotidienne

| Dossier dans l'archive | Données sauvegardées | Méthode |
| :--- | :--- | :--- |
| `postgres/` | `powertrack.dump`, `powertrack.sql`, `carriere.dump`, `carriere.sql`, `db_entrainement.dump` | `pg_dump -Fc` et SQL clair (`--no-owner`) via `postgres-force` |
| `n8n/` | `database.sqlite`, `config`, `docker-compose.yml`, `workflows.json` | Copie SQLite cohérente (`sqlite3.Connection.backup()`) + `n8n export:workflow --all` |
| `grafana/` | `grafana.db` | Copie SQLite cohérente via API Python `sqlite3` |
| `npm/` | `database.sqlite`, `custom_ssl/` | Copie SQLite cohérente + certificats personnalisés NPM |
| `homelab-ca/` | PKI complète (`homelab-root-ca.*`, `wildcard.homelab.*`, `.cnf`) | Copie directe préservant les permissions (`cp -a`) |
| `homepage/` | `docker-compose.yml`, `.env`, `config/` (YAML Homepage) | Copie directe préservant les permissions (`cp -a`) |
| `carriere/` | `Dockerfile`, `docker-compose.yml`, `.env`, `init-carriere.sql`, `app/` | Copie directe préservant les permissions (`cp -a`) |
| `carnet-force/`, `monitoring/`, `gitea/`, `adguard/`, `tts/`, `configs/` | `docker-compose.yml`, `.env`, `init-powertrack.sql`, `prometheus.yml`, `AdGuardHome.yaml`, `tts-docker/{Dockerfile,docker-compose.yml,server.py}`, scripts `/usr/local/sbin/homelab-*`, unités `systemd/homelab-*`, `51homelab-unattended` | Copie directe |

---

## 2 bis. Alertes automatiques (échec backup, TLS & disque)

- En cas d'échec de `homelab-backup.service`, `OnFailure=homelab-backup-alert.service` déclenche [`homelab-health-alert.sh`](../backups/homelab-health-alert.sh) (`--backup-failed`) pour envoyer les 10 dernières lignes `journalctl` vers Discord via `n8n`.
- Chaque matin à `08:00` (`homelab-cert-check.timer`), `--daily-check` vérifie que `latest.tar.gz` a moins de 26 h, que `wildcard.homelab.crt` expire dans plus de 30 jours et que `/` est sous 85 % d'occupation.

---

## 3. Vérification et procédures de restauration

### Contrôler le dernier backup et sa table des matières
```bash
systemctl status homelab-backup.timer
tar -tzf /home/ethan/backups/homelab/latest.tar.gz | sort
tar -xOzf /home/ethan/backups/homelab/latest.tar.gz ./postgres/powertrack.dump \
  | docker exec -i postgres-force pg_restore --list | head -n 30
```

### Restaurer la base PostgreSQL `powertrack`
```bash
tar -xOzf /home/ethan/backups/homelab/latest.tar.gz ./postgres/powertrack.dump \
  | docker exec -i postgres-force pg_restore -U ethan -d powertrack --clean --if-exists --no-owner
```

### Restaurer `n8n` ou `Grafana`
1. Stopper le conteneur concerné (`docker stop n8n` ou `docker stop grafana`).
2. Extraire le fichier SQLite (et `./n8n/config` pour `n8n`, indispensable au déchiffrement des credentials enregistrés) vers le répertoire du volume Docker correspondant.
3. Redémarrer le conteneur (`docker start n8n` ou `docker start grafana`).

### Lancer une sauvegarde manuelle immédiate
```bash
sudo systemctl start homelab-backup.service
sudo journalctl -u homelab-backup.service -n 50 --no-pager
```
