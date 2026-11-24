# Audit Lynis & Pipeline DevSecOps CI

Ce document résume le durcissement système mesuré sur **`Serveur-Linux` (VM 101, Ubuntu 26.04.1 LTS)** via **Lynis `3.1.6`**, ainsi que la chaîne **DevSecOps CI** exécutée sur GitHub Actions.

---

## 1. Résultats d'audit Lynis (avant → après durcissement)

| Métrique | Avant durcissement | Après durcissement | Évolution |
| :--- | :---: | :---: | :---: |
| **Hardening Index (`lynis`)** | `64` | **`74`** | **`+10` points** |
| **Tests exécutés** | `265` | `265` | — |
| **Suggestions Lynis** | `49` | **`36`** | **`-13` suggestions** |
| **Surcoût RAM** | — | **`0 Mo`** | Aucun daemon ajouté |

*Avertissements résiduels connus (`2`) : `KRNL-5830` (redémarrage noyau ultérieur possible hors fenêtre) et `NETW-2705` (1 résolveur DNS configuré côté Netplan).*

---

## 2. Contrôles de durcissement appliqués (`system/hardening/`)

1. **Durcissement SSH (`10-homelab-hardening.conf` → `/etc/ssh/sshd_config.d/`)**
   - Préfixé `10-` pour primer sur `/etc/ssh/sshd_config.d/50-cloud-init.conf` (`PasswordAuthentication yes`).
   - Authentification par clé uniquement (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `AuthenticationMethods publickey`, `PermitRootLogin no`).
   - Surface réduite : `MaxAuthTries 3`, `MaxSessions 4`, `LoginGraceTime 30`, `X11Forwarding no`, `AllowAgentForwarding no`, `AllowTcpForwarding local`, `LogLevel VERBOSE`, bannière légale `/etc/issue.net`.
2. **Noyau & pile réseau (`98-homelab-hardening.conf` → `/etc/sysctl.d/`)**
   - `kernel.kptr_restrict = 2`, `kernel.dmesg_restrict = 1`, `kernel.yama.ptrace_scope = 1`, `kernel.sysrq = 0`, `kernel.unprivileged_bpf_disabled = 1`, `net.core.bpf_jit_harden = 2`.
   - Protections FS : `fs.suid_dumpable = 0`, `fs.protected_hardlinks = 1`, `fs.protected_symlinks = 1`, `fs.protected_fifos = 2`, `fs.protected_regular = 2`.
   - Réseau IPv4/IPv6 : `rp_filter = 1`, `tcp_syncookies = 1`, `tcp_rfc1337 = 1`, `log_martians = 1`, désactivation des redirections ICMP et du source routing (tout en préservant `net.ipv4.ip_forward = 1` indispensable au réseau Docker).
3. **Surface noyau & core dumps**
   - [`99-homelab-blacklist.conf`](../system/hardening/99-homelab-blacklist.conf) : désactivation des protocoles inutilisés (`dccp`, `sctp`, `rds`, `tipc`) et systèmes de fichiers exotiques (`cramfs`, `freevxfs`, `hfs`, `hfsplus`, `udf`).
   - [`99-homelab-nocoredump.conf`](../system/hardening/99-homelab-nocoredump.conf) : `* hard core 0`.
4. **Permissions & umask système**
   - Permissions restreintes sur `/etc/crontab`, `/etc/ssh/sshd_config` (`600`) et `/etc/cron.*` (`700`).
   - `UMASK 027` dans `/etc/login.defs`.
5. **Défense en profondeur existante**
   - Pare-feu `UFW` + `nftables`, `Fail2ban` (`sshd` + alerte Discord immédiate), SSO **Authelia** (`*.homelab.lan`), et centralisation + détection LogQL (`Loki` + `Grafana Alloy` + `homelab-security-watch.timer`).

L'ensemble de ces fichiers de configuration est sauvegardé automatiquement dans l'archive chiffrable quotidienne produite par [`backups/homelab-backup.sh`](../backups/homelab-backup.sh).

---

## 3. Pipeline DevSecOps CI ([`.github/workflows/devsecops.yml`](../.github/workflows/devsecops.yml))

Déclenché à chaque `push` / `pull_request` sur `main` et chaque lundi à `07:00 UTC`, avec le principe du **moindre privilège** (`permissions: contents: read`) et exécution via les **conteneurs officiels GHCR** (réduction du risque *supply chain* par rapport à des actions tierces) :

| Job CI | Outil | Portée |
| :--- | :--- | :--- |
| **`secret-scan`** | `ghcr.io/gitleaks/gitleaks:latest` | Analyse complète de l'historique Git (`.gitleaks.toml`) |
| **`shell-and-config-lint`** | `bash -n`, `ShellCheck`, Python JSON/YAML, `docker compose config -q` | Validation syntaxique et statique des scripts, dashboards, YAML et des 10 stacks Compose |
| **`trivy-security-scan`** | `ghcr.io/aquasecurity/trivy:latest` | Détection des mauvaises configurations IaC (`HIGH,CRITICAL`) et vulnérabilités/secrets (`CRITICAL`) avec exceptions documentées dans `.trivyignore` |
