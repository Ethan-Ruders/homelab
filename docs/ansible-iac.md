# Infrastructure as Code (Ansible)

L'automatisation de **`Serveur-Linux` (`10.0.0.74`)** repose sur **Ansible**, exécuté directement depuis le poste Mac via SSH (**agentless**, **`0 Mo` de RAM** consommé sur le serveur).

Chaque fichier déployé provient directement des répertoires canoniques du dépôt ([`system/`](../system/), [`backups/`](../backups/), [`stacks/`](../stacks/)) afin d'éviter toute duplication et toute dérive entre Git et le serveur réel.

---

## Structure Ansible

- [`ansible/ansible.cfg`](../ansible/ansible.cfg) : inventaire par défaut, pipelining SSH activé, vérification stricte de clé hôte (`host_key_checking = True`).
- [`ansible/inventory/hosts.yml`](../ansible/inventory/hosts.yml) : hôte cible `srvlinux` (`10.0.0.74`, utilisateur `ethan`).
- [`ansible/inventory/group_vars/serveur_linux.yml`](../ansible/inventory/group_vars/serveur_linux.yml) : paquets, règles UFW, unités systemd et cartographie complète des stacks.
- [`ansible/playbooks/site.yml`](../ansible/playbooks/site.yml) : playbook principal orchestrant les 5 rôles ci-dessous.

---

## Rôles et périmètre géré

| Rôle | Tags | Ce qui est configuré sur `Serveur-Linux` |
| :--- | :--- | :--- |
| `base` | `base` | Paquets essentiels (`ca-certificates`, `curl`, `fail2ban`, `git`, `gnupg`, `jq`, `lynis`, `python3`, `ufw`, `unattended-upgrades`), `51homelab-unattended`, `99-homelab.conf` (`vm.swappiness=10`) |
| `docker` | `docker` | Dépôt APT officiel Docker + clé GPG, installation `docker-ce` / `docker-compose-plugin`, appartenance `ethan` au groupe `docker`, `daemon.json` (rotation logs), réseau bridge `homelab-net` |
| `hardening` | `hardening`, `security` | SSH durci (`10-homelab-hardening.conf` avec validation `sshd -t`, bannière `issue.net`, `ssh.socket`), `98-homelab-hardening.conf` (`sysctl`), blacklist `modprobe`, désactivation core dumps, permissions `cron` & `UMASK 027`, règles `UFW`, jail & alerte Discord `Fail2ban` |
| `operations` | `operations`, `backups` | Dossiers `/home/ethan/backups/homelab` (`0700`), scripts `/usr/local/sbin/homelab-backup.sh` & `homelab-health-alert.sh`, unités et timers `systemd` (`homelab-backup`, `homelab-cert-check`, `homelab-security-watch`) |
| `stacks` | `stacks` | Arborescence Docker sous `/home/ethan/*`, synchronisation des `docker-compose.yml`, configs (`monitoring`, `homepage`, `authelia`, `npm-custom`) et redéploiement optionnel des stacks (`-e homelab_apply_stacks=true`) |

### Ce qui reste volontairement hors Ansible & Git
Pour garder les secrets et données persistantes strictement hors du dépôt Git :
- Fichiers `.env`, dossiers `secrets/`, `users_database.yml` (`Authelia`)
- Certificats & clés privées (`homelab-ca/`)
- Données runtime (`postgres-data/`, bases SQLite `n8n` / `grafana` / `npm`, `AdGuardHome.yaml`)

Ces éléments sont sauvegardés quotidiennement dans `/home/ethan/backups/homelab/latest.tar.gz` (voir [la stratégie de sauvegarde et restauration](backup-restore.md)).

---

## Commandes courantes (depuis `ansible/`)

```bash
cd /Users/ethanruders/homelab/ansible
```

### 1. Vérifier la dérive sans rien modifier (`--check --diff`)
```bash
ansible-playbook playbooks/site.yml --check --diff
```

### 2. Appliquer la configuration sur `Serveur-Linux`
```bash
ansible-playbook playbooks/site.yml --diff
```

### 3. Exécuter uniquement un périmètre ciblé (`--tags`)
```bash
ansible-playbook playbooks/site.yml --tags hardening --diff
ansible-playbook playbooks/site.yml --tags stacks --diff
```

### 4. Reprise après sinistre (PRA / reconstruction complète)
Une fois l'OS Ubuntu installé, la clé SSH posée et les secrets/backups restaurés :
```bash
ansible-playbook playbooks/site.yml -e homelab_apply_stacks=true --diff
```
