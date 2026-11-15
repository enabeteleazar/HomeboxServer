# 🏠 HOMEBOX — documentation du serveur

Serveur de développement et d'hébergement : outils de dev, bases de données en
conteneurs, publication web, sauvegardes, surveillance et alertes.

Cette page décrit **la machine et son organisation**. Le détail des scripts est dans
[infra/README.md](../infra/README.md), celui de l'interface web dans
[dashboard/README.md](../dashboard/README.md).

> Relevé du 15/11/2026. Les versions bougent avec les mises à jour : le dashboard
> (page Système) donne toujours l'état réel.

---

## 1. Machine

| Élément | Valeur |
|---|---|
| Nom | `homebox` |
| Système | Ubuntu 26.04.1 LTS, noyau 7.0 |
| Processeur | Intel Pentium G3240T, 2 cœurs, 2,7 GHz |
| Mémoire | 7,2 Go + 8 Go de swap |
| Disque | **un seul** disque mécanique 2,5" HGST HTS725050A7E630, 500 Go, 7200 tr/min |
| Réseau | Wi-Fi (clé USB `wlx…`, 192.168.1.22), Ethernet `eno1` (non utilisé), Tailscale 100.73.78.48 |
| Utilisateur | `eleazar` (groupe `docker`, équivalent root) |

Partitions (toutes sur le même disque) :

| Point de montage | Taille | Contenu |
|---|---|---|
| `/` | 40 Go | système |
| `/var` | 40 Go | Docker (images, volumes : données PostgreSQL et Redis), journaux |
| `/home` | 80 Go | dossier personnel, caches npm/pnpm/Vercel |
| `/srv` | 290 Go | projets, configuration Docker, sauvegardes locales |
| `/boot`, `/boot/efi`, swap | 1 + 1 + 8 Go | |

⚠️ **Un seul disque mécanique** : tout (système, données, sauvegardes locales) disparaît
avec lui. Sa santé est relevée toutes les heures (SMART, voir §9) et la **copie externe
des sauvegardes** (§8) est la vraie protection.

---

## 2. Organisation de /srv

```
/srv/
├── Makefile -> HomeboxServer/dashboard/Makefile   gestion des projets : make list, make dev <projet>…
├── HomeboxServer/        ce dépôt : infra/ (scripts root) + dashboard/ (interface web) + docs/
├── Docker/               compose.yml + .env (identifiants des bases) — ne jamais publier .env
├── Projects/             les projets web, un dépôt git chacun (registre du dashboard)
├── Neron/                l'écosystème Neron (racine « neron », ports 8000-8099)
├── backups/homebox/      sauvegardes locales (root, 0700)
├── .env                  ports des projets, jeton API et mot de passe (empreinte) du dashboard
├── .data/                journal d'activité et historique des mesures (SQLite)
└── README.md -> HomeboxServer/docs/serveur.md      cette page
```

Ailleurs : `/etc/homebox/` (réglages et secrets des outils, root), `/var/lib/homebox/`
(état publié pour le dashboard), `/usr/local/sbin/homebox-*` (outils root).

---

## 3. Environnement de développement

| Logiciel | Version | Rôle |
|---|---|---|
| Node.js | 24.22 | exécution JavaScript |
| npm / pnpm | 12.2 / 12.16 | gestionnaires de paquets |
| Vercel CLI | 65.12 | déploiement des sites |
| Git / Git Flow | 2.53 / 1.12 AVH | code source, branches |
| GitHub CLI | 2.104 | dépôts, PR (`gh auth status`) |
| Python | 3.14 | dashboard, outils homebox-* |
| Docker / Compose | 29.9 / 5.6 | conteneurs |
| Caddy | 2.11 | serveur web, HTTPS automatique |
| Tailscale | 1.104 | accès distant privé |
| restic | 0.18 | copie externe chiffrée des sauvegardes |

Installés par `make install-node`. Clients `psql` et `redis-cli` présents ; PostgreSQL et
Redis **système** sont désactivés (doublons des conteneurs, voir §5).

---

## 4. Infrastructure Docker

Configuration : `/srv/Docker/compose.yml` + `/srv/Docker/.env` (projet Compose `homebox`).

| Service | Image | Conteneur | Port (127.0.0.1) |
|---|---|---|---|
| PostgreSQL 18 | `postgres:18` | `homebox-postgres` | 5433 |
| Redis 8 | `redis:8-alpine` | `homebox-redis` | 6380 |
| Adminer | `adminer:5` | `homebox-adminer` | 8080 |
| Redis Commander | `ghcr.io/joeferner/redis-commander:0.9` | `homebox-redis-commander` | 8081 |
| Uptime Kuma | `louislam/uptime-kuma:2` | `homebox-uptime-kuma` | 3001 |

- Images figées sur une version majeure : `make update-check` / `make update` appliquent
  les correctifs (après une sauvegarde).
- Réseau `homebox-network` : depuis un conteneur, `postgres:5432` et `redis:6379`.
- Volumes `homebox-postgres-data`, `homebox-redis-data`, `homebox-uptime-kuma-data`
  (dans `/var/lib/docker`). **Ne jamais lancer `docker compose down -v`** : `-v` efface les données.
- Docker publie par défaut sur `127.0.0.1` (`/etc/docker/daemon.json`) : il contourne UFW,
  un port publié sans adresse serait ouvert sur Internet malgré le pare-feu.

Commandes (sans sudo, l'utilisateur est dans le groupe docker) :

```bash
cd /srv/HomeboxServer && make ps          # état des conteneurs
make logs SERVICE=postgres                # journaux d'un service
docker compose --project-name homebox --env-file /srv/Docker/.env -f /srv/Docker/compose.yml restart redis
```

---

## 5. Bases de données

**PostgreSQL** (`homebox-postgres`, `127.0.0.1:5433`). Chaque projet a sa base et son
utilisateur, sans accès aux autres :

```bash
make project-create NAME=monprojet   # affiche DATABASE_URL (une seule fois)
make project-list
```

Le compte `homebox` (mot de passe dans `/srv/Docker/.env`) est réservé à l'administration.

**Redis** (`homebox-redis`, `127.0.0.1:6380`, AOF activé, mot de passe dans le `.env`).
Partagé : préfixer les clés par le nom du projet.

**PostgreSQL et Redis système** (ports 5432 / 6379) : arrêtés et désactivés le
15/11/2026, données conservées dans `/var/lib/postgresql` (dont une base `stockmonsters`).
Réactiver au besoin : `sudo systemctl enable --now postgresql`.

---

## 6. Ports et accès

| Port | Service | Joignable depuis |
|---|---|---|
| 22 | SSH | partout (limité contre la force brute) |
| 80 / 443 | Caddy (sites publiés) | partout |
| 5199 | Dashboard | réseau local + Tailscale (mot de passe) |
| 5180-5299 | serveurs de dev des projets | réseau local + Tailscale |
| 8443 | Tailscale serve | Tailscale |
| 5433, 6380, 8080, 8081, 3001 | bases, Adminer, Redis Commander, Uptime Kuma | machine seulement |

L'exposition **réelle** de chaque port (règles et Docker compris) est sur la page
Pare-feu du dashboard. Interfaces locales depuis ton poste :

```bash
ssh -L 8080:localhost:8080 -L 8081:localhost:8081 -L 3001:localhost:3001 eleazar@homebox
# Adminer http://localhost:8080 · Redis Commander :8081 · Uptime Kuma :3001
```

Dashboard : `http://homebox:5199/overview` (ou l'adresse Tailscale).

---

## 7. Sécurité

| Mesure | Comment | Commande |
|---|---|---|
| Pare-feu UFW | tout refusé en entrée sauf SSH (limité), 80/443, réseau local, Tailscale | `make firewall` |
| SSH par clés | mots de passe et root par mot de passe refusés | `ssh-copy-id` depuis ton poste, puis `make harden-ssh` |
| Fail2ban | bannit les adresses qui ratent trop de connexions SSH | actif |
| Mises à jour de sécurité | unattended-upgrades | actif |
| Dashboard | mot de passe (scrypt), sessions 7 j, anti force brute, CSRF | `make dashboard-password` |
| Droits root du dashboard | sudo limité à `homebox-firewall` et deux `systemctl start` | `/etc/sudoers.d/homebox-firewall` |

Le groupe `docker` donne des droits équivalents à root : n'y ajouter personne d'autre.

---

## 8. Sauvegardes

Automatiques chaque nuit vers 03:30 (`homebox-backup.timer`), dans
`/srv/backups/homebox/AAAAMMJJ-HHMMSS/`, conservées 14 jours :
toutes les bases PostgreSQL, les rôles, Redis, Uptime Kuma, le `.env`, sommes SHA256.

- **Copie externe** chiffrée (restic : disque USB, NAS, Backblaze B2, S3) après chaque
  sauvegarde, une fois configurée : `sudo nano /etc/homebox/restic.env` puis `make offsite-init`.
- **Test de restauration** à blanc le 1er de chaque mois, dans des conteneurs jetables.
- Page Sauvegardes du dashboard : état, liste, bouton « Sauvegarder maintenant ».

```bash
make backup               # sauvegarde immédiate
make backups              # liste
make restore-test         # restauration à blanc, sans risque
sudo homebox-restore postgres AAAAMMJJ-HHMMSS monprojet   # restauration réelle d'une base
```

---

## 9. Surveillance et alertes

| Quoi | Où | Fréquence |
|---|---|---|
| CPU, mémoire, disques, réseau, températures, processus | dashboard, page Système | en direct |
| Historique et tendances (remplissage des disques) | dashboard, page Historique | mesure toutes les 15 s, 90 jours |
| Conteneurs, services, tâches planifiées | dashboard, pages Docker et Services | en direct |
| Santé SMART du disque (+ auto-test hebdomadaire) | `homebox-smart`, page Système | toutes les heures |
| Parcage des têtes limité (APM 254) | `homebox-disk-apm` (`make disk-apm`) | à chaque démarrage |
| Contrôle de santé : disque, conteneurs, sauvegardes, pare-feu, SSH, seuils soutenus, certificats HTTPS | `homebox-healthcheck`, page Vue d'ensemble | toutes les 15 min |
| Disponibilité des sites | Uptime Kuma | à configurer |

Les alertes partent sur le téléphone via **ntfy** (sujet dans `/etc/homebox/homebox.conf`,
test : `make notify-test`), à l'apparition d'un problème puis en rappel toutes les 24 h.

---

## 10. Projets et publication

- Les projets vivent dans `/srv/Projects/<Nom>/`. `make sync` (ou le bouton Sync du
  dashboard) enregistre les nouveaux dossiers, attribue un port (5180-5299) et retire ceux
  qui ont disparu.
- `cd /srv && make list`, `make dev <projet>`, `make deploy <projet>` (Vercel).
- Publier un projet en HTTPS : page **Caddy** du dashboard (« Publier un site » : un projet,
  un port ou un dossier statique), ou en ligne de commande, sur un domaine qui pointe vers la machine :

```bash
make site-add DOMAIN=app.exemple.fr PORT=5190
make site-static DOMAIN=doc.exemple.fr DIR=/srv/Projects/Doc/dist
```

Modèle pour un projet en conteneur : `infra/files/templates/app-compose.yml`.

---

## 11. Vérifications rapides

```bash
cd /srv/HomeboxServer
make status               # conteneurs, tâches planifiées, contrôle de santé
make health               # contrôle de santé immédiat
systemctl list-timers 'homebox-*'
sudo ss -tulpn            # ports en écoute
sudo ufw status verbose
```

---

## 12. Points en attente (au 15/11/2026)

- [ ] **Copie externe des sauvegardes** à configurer (§8) — priorité, le disque est unique et âgé.
- [ ] **Clé SSH** à ajouter (`ssh-copy-id` depuis ton poste), puis `make harden-ssh`.
- [ ] **Pare-feu** à activer : `make firewall` (ou bouton sur la page Pare-feu).
- [ ] **Disque** : 376 secteurs réalloués, ≈ 1,9 million de parcages des têtes (≈ 3× l'usure
      prévue), 4,4 ans de fonctionnement — fonctionne, mais en fin de vie. Parcage limité
      depuis le 15/11 (`homebox-disk-apm`) ; prévoir son remplacement (SSD) et un second disque.
- [ ] Mises à jour système en attente : `sudo apt upgrade`.
- [ ] Anciennes sauvegardes du 31/10 au 15/11 supprimées : la première nouvelle sauvegarde
      sera celle de cette nuit (ou `make backup`).
- [ ] Uptime Kuma : créer le compte admin et une sonde par site.
