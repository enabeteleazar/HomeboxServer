# HOMEBOX : serveur de développement

Kit d'installation d'un serveur de développement perso sous Ubuntu 26.04 :
outils de dev, bases de données en conteneurs, publication web via Caddy,
sauvegardes locales et externes, surveillance et alertes.

## Contenu

```
HomeboxServer/              (/srv/Projects/HomeboxServer)
├── install-node.sh        # 1. prépare la machine (outils, Docker, Caddy, pare-feu)
├── infra-docker.sh        # 2. bases de données, sauvegardes, surveillance (relançable)
├── firewall.sh            # pare-feu UFW (réseau local + Tailscale, Internet bloqué)
├── harden-ssh.sh          # SSH par clés uniquement
├── remove-system-db.sh    # désactive PostgreSQL/Redis installés via apt (doublons)
└── files/
    ├── compose.yml        # PostgreSQL, Redis, Adminer, Redis Commander, Uptime Kuma
    ├── bin/homebox-*      # outils installés dans /usr/local/sbin (et bin)
    ├── lib/common.sh      # fonctions partagées par les outils
    ├── systemd/           # tâches planifiées
    ├── config/            # modèles de /etc/homebox/
    ├── caddy/Caddyfile    # configuration Caddy (un fichier par site)
    └── templates/         # modèle compose.yml pour déployer un projet
```

## Raccourcis (Makefile)

Toutes les commandes de ce README existent aussi en version courte :

```bash
make                 # liste des commandes
make status          # conteneurs, tâches planifiées, santé
make backup          # sauvegarde immédiate
make project-create NAME=monprojet
make site-add DOMAIN=app.exemple.fr PORT=5190
make logs SERVICE=postgres
```

## Installation sur une machine neuve

```bash
# Depuis ton poste, d'abord : ssh-copy-id eleazar@homebox
sudo bash ./install-node.sh
sudo bash ./infra-docker.sh
```

Options :

```bash
sudo INSTALL_SYSTEM_DB=true bash ./install-node.sh        # PostgreSQL/Redis système en plus (déconseillé)
sudo SSH_TAILSCALE_ONLY=true bash ./install-node.sh       # SSH joignable uniquement via Tailscale
sudo BACKUP_DIR=/mnt/disque2/backups RETENTION_DAYS=30 bash ./infra-docker.sh
```

## Mise à niveau d'une machine déjà installée

Dans cet ordre :

1. **Clé SSH** depuis ton poste : `ssh-copy-id eleazar@homebox`, puis vérifie
   que `ssh eleazar@homebox` se connecte sans demander de mot de passe.
2. `sudo bash ./remove-system-db.sh` : sauvegarde puis désactive les PostgreSQL
   et Redis système (ports 5432 et 6379), qui faisaient doublon avec Docker.
3. `sudo bash ./infra-docker.sh` : met à jour `compose.yml` (l'ancien est gardé en
   `compose.yml.bak-*`), ajoute Uptime Kuma, installe les outils et les tâches planifiées.
4. `sudo bash ./harden-ssh.sh` : garde ta session ouverte et teste une nouvelle connexion.
5. **Notifications** : installe l'app ntfy, abonne-toi au sujet affiché par
   infra-docker.sh, puis `sudo homebox-notify --test`.
6. **Copie externe** : voir plus bas.
7. `sudo homebox-update` : applique les correctifs d'images disponibles.

Les scripts sont relançables : le `.env` et `/etc/homebox/homebox.conf` existants
sont conservés, un `compose.yml` ou un `Caddyfile` modifié à la main n'est jamais
écrasé (la nouvelle version est déposée à côté en `.new`).

## Ce qui est installé

| Élément | Emplacement |
|---|---|
| Compose et `.env` (identifiants) | `/srv/docker/` |
| Réglages (ntfy, seuils d'alerte) | `/etc/homebox/homebox.conf` |
| Copie externe (restic) | `/etc/homebox/restic.env`, `/etc/homebox/restic.pass` |
| Outils root | `/usr/local/sbin/homebox-*` |
| Outil utilisateur | `/usr/local/bin/homebox-project` |
| Sauvegardes locales | `/srv/backups/homebox/AAAAMMJJ-HHMMSS/` (root, 0700) |
| Sites Caddy | `/etc/caddy/sites/<domaine>.caddy` |
| Logs d'installation | `/var/log/homebox-docker-*.log` |

| Tâche planifiée | Quand |
|---|---|
| `homebox-backup.timer` : sauvegarde locale + copie externe | tous les jours à 03:30 |
| `homebox-healthcheck.timer` : contrôle de santé + alertes | toutes les 15 min |
| `homebox-restore-test.timer` : restauration à blanc + vérification restic | le 1er du mois |
| `homebox-smart.timer` : santé SMART des disques (+ auto-test court hebdomadaire) | toutes les heures |

Toute tâche qui échoue envoie une notification (`homebox-failure@.service`).

**Santé des disques** : `homebox-smart` écrit `/var/lib/homebox/smart.json` (lu par le
dashboard) et retient les compteurs précédents. Secteurs réalloués ou en attente en
hausse, auto-test échoué ou panne annoncée par le disque : `homebox-healthcheck`
envoie une alerte. Relevé manuel : `sudo homebox-smart --no-test`.

## Accès

Tous les ports sont liés à `127.0.0.1`. Depuis ton poste :

```bash
ssh -L 8080:localhost:8080 -L 8081:localhost:8081 -L 3001:localhost:3001 eleazar@homebox
```

| Service | Adresse |
|---|---|
| PostgreSQL | `localhost:5433` |
| Redis | `localhost:6380` |
| Adminer | http://localhost:8080 |
| Redis Commander | http://localhost:8081 |
| Uptime Kuma | http://localhost:3001 (crée le compte admin dès la première visite) |

N'expose jamais ces interfaces via Caddy sans authentification.

## Une base par projet

Les projets n'utilisent pas le compte administrateur `homebox` : chacun a sa
base et son utilisateur, sans accès aux autres bases.

```bash
homebox-project create monprojet     # affiche DATABASE_URL (une seule fois)
homebox-project list
homebox-project password monprojet   # nouveau mot de passe
homebox-project drop monprojet
```

Redis est partagé : préfixe les clés par le nom du projet (`monprojet:...`).

## Publier un projet

Les projets tournent en conteneurs (ou en processus) sur `127.0.0.1`, Caddy les
publie en HTTPS. Modèle : `files/templates/app-compose.yml`.

```bash
sudo homebox-site add monprojet.example.com 5190              # reverse proxy
sudo homebox-site add doc.example.com --static /srv/Projects/doc/dist
sudo homebox-site list
sudo homebox-site remove monprojet.example.com
```

Le domaine doit pointer (DNS) vers la machine. Renseigne l'`email` dans
`/etc/caddy/Caddyfile` pour Let's Encrypt.

**Pare-feu** : `sudo bash ./firewall.sh` (ou `make firewall`) refuse tout ce qui
entre, sauf SSH (limité), 80/443 pour Caddy, le réseau local et Tailscale. Le
dashboard (5199) et les serveurs de dev restent donc joignables chez toi et via
Tailscale, pas depuis Internet. `make firewall-check` montre les règles sans
les appliquer ; options `TRUST_LAN`, `TRUST_TAILSCALE`, `OPEN_WEB`, `SSH_TAILSCALE_ONLY`.

**Docker et pare-feu** : Docker contourne UFW. Un port publié sans adresse
(`-p 5432:5432`) serait ouvert sur Internet malgré le pare-feu.
`install-node.sh` règle `"ip": "127.0.0.1"` dans `/etc/docker/daemon.json` pour
que ce soit `127.0.0.1` par défaut, et `homebox-healthcheck` alerte si un
conteneur publie quand même un port sur toutes les interfaces.

## Sauvegardes

Chaque sauvegarde contient : `postgres/<base>.dump` (toutes les bases),
`postgres-globals.sql` (rôles), `redis.rdb`, `uptime-kuma.tar.gz`, `env.backup`
et `SHA256SUMS`. Les secrets sont dedans.

```bash
sudo systemctl start homebox-backup.service   # sauvegarde immédiate
sudo journalctl -u homebox-backup.service     # historique
sudo homebox-restore list
```

### Copie externe (indispensable)

Les sauvegardes locales protègent d'une erreur de manipulation, pas d'une panne
de disque. La copie externe utilise restic : chiffrée, dédupliquée, conservée
7 jours, 4 semaines et 6 mois.

```bash
sudo nano /etc/homebox/restic.env      # choisir la destination (USB, NAS, B2, S3)
sudo homebox-offsite init              # crée le dépôt et son mot de passe
sudo cat /etc/homebox/restic.pass      # à copier dans ton gestionnaire de mots de passe
sudo systemctl start homebox-backup.service
sudo homebox-offsite snapshots
```

**Sans `restic.pass`, la copie externe est illisible** : garde-le hors de la machine.

## Restauration

```bash
sudo homebox-restore verify                 # contrôle les sommes SHA256
sudo homebox-restore test                   # restauration à blanc, sans risque
sudo homebox-restore postgres               # toutes les bases de la dernière sauvegarde
sudo homebox-restore postgres 20261115-033012 monprojet   # une seule base
sudo homebox-restore redis
```

Avant d'écraser une base, l'état actuel est copié dans
`/srv/backups/homebox/pre-restore-*.dump` (supprimé après la durée de rétention).
Pour Redis, l'ancien contenu reste dans le volume (`pre-restore-*`).

**Sinistre complet** (machine perdue) :

1. Réinstalle avec `install-node.sh`, recrée `/etc/homebox/restic.env` et `restic.pass`.
2. `sudo homebox-offsite restore latest /root/recup`
3. Copie `env.backup` en `/srv/docker/.env` (le mot de passe PostgreSQL n'est lu
   qu'à l'initialisation du volume), puis `sudo bash ./infra-docker.sh`.
4. `sudo homebox-restore postgres /root/recup/srv/backups/homebox/<horodatage>`,
   puis `sudo homebox-restore redis` avec le même chemin.

## Surveillance et alertes

`homebox-healthcheck` vérifie l'espace disque, l'état des conteneurs, les ports
exposés, l'âge des sauvegardes (locales et externes), le pare-feu et la
configuration SSH. Il prévient à l'apparition d'un problème, le rappelle toutes
les 24 h, et signale le retour à la normale.

```bash
sudo homebox-healthcheck --no-notify    # rapport immédiat
sudo nano /etc/homebox/homebox.conf     # ntfy, seuils, exceptions
```

Uptime Kuma complète ce contrôle : surveillance de tes sites (HTTP), historique
de disponibilité. Configure-lui aussi une notification ntfy dans son interface.

## Mises à jour

`unattended-upgrades` met à jour Ubuntu, pas les conteneurs. Les images sont
figées sur une version majeure (`postgres:18`, `redis:8`, `adminer:5`,
`redis-commander:0.9`, `uptime-kuma:2`) : seuls les correctifs sont appliqués.

```bash
sudo homebox-update --check   # mises à jour disponibles ?
sudo homebox-update           # sauvegarde, puis mise à jour et redémarrage
```

Une mise à jour majeure de PostgreSQL (18 vers 19) demande un `pg_upgrade` ou
une sauvegarde/restauration, pas un simple changement de tag.

## Commandes utiles

```bash
cd /srv/docker
docker compose --env-file .env ps
docker compose --env-file .env logs -f postgres
systemctl list-timers 'homebox-*'
```
