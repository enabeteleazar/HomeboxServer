# HOMEBOX Docker Infrastructure

PostgreSQL 18, Redis 8, Adminer et Redis Commander, avec sauvegardes quotidiennes.

## Contenu

```
homebox-docker/
├── infra-docker.sh      # installeur (à lancer avec sudo)
├── files/
│   ├── compose.yml      # services Docker
│   └── backup.sh        # script de sauvegarde (installé en root)
└── README.md
```

## Installation

Prérequis : Docker, Docker Compose et OpenSSL (installés par `install-node.sh`).

```bash
unzip homebox-docker.zip
cd homebox-docker
sudo bash ./infra-docker.sh
```

Options (variables d'environnement) :

```bash
sudo BACKUP_DIR=/mnt/disque2/backups RETENTION_DAYS=30 bash ./infra-docker.sh
```

Le script est relançable : le `.env` existant est conservé, et un `compose.yml`
modifié à la main n'est jamais écrasé (la nouvelle version est déposée dans
`compose.yml.new`).

## Ce qui est installé

| Élément | Emplacement |
|---|---|
| Compose et `.env` | `/srv/docker/` |
| Script de sauvegarde | `/usr/local/sbin/homebox-backup` (root) |
| Timer systemd | `homebox-backup.timer`, tous les jours à 03:30 |
| Sauvegardes | `/srv/backups/homebox/AAAAMMJJ-HHMMSS/` (root, 0700) |
| Logs d'installation | `/var/log/homebox-docker-*.log` |

Chaque sauvegarde contient : `postgres.dump`, `postgres-globals.sql`,
`redis.rdb`, `env.backup` et `SHA256SUMS`. Les secrets sont dedans :
ne les copie que vers un support chiffré ou de confiance.

## Accès

Tous les ports sont liés à `127.0.0.1`. Depuis ton poste :

```bash
ssh -L 8080:localhost:8080 -L 8081:localhost:8081 eleazar@homebox
```

Puis `http://localhost:8080` (Adminer) et `http://localhost:8081` (Redis Commander).
N'expose jamais ces interfaces via Caddy sans authentification.

## Commandes utiles

```bash
cd /srv/docker
docker compose --env-file .env ps
docker compose --env-file .env logs -f postgres

sudo systemctl start homebox-backup.service     # sauvegarde immédiate
sudo systemctl list-timers homebox-backup.timer # prochaine exécution
sudo journalctl -u homebox-backup.service       # historique
```

## Restauration

**PostgreSQL** (remplace le contenu de la base) :

```bash
B=/srv/backups/homebox/AAAAMMJJ-HHMMSS
sudo sha256sum -c $B/SHA256SUMS
sudo docker exec -i homebox-postgres \
  pg_restore -U homebox -d homebox --clean --if-exists < $B/postgres.dump
```

**Redis** (à tester une fois à blanc : avec `appendonly yes`, Redis charge
l'AOF en priorité sur le RDB) :

1. `docker compose --env-file .env stop redis`
2. Vider le volume `homebox-redis-data`, y copier `redis.rdb` sous le nom `dump.rdb`
3. Démarrer Redis une première fois avec `--appendonly no`, puis rétablir la config normale

**Sinistre complet** : restaure d'abord `env.backup` en `/srv/docker/.env`
(le mot de passe PostgreSQL n'est lu qu'à l'initialisation du volume),
relance `infra-docker.sh`, puis restaure le dump.

## Points d'attention

- **Doublon possible** : `install-node.sh` installe aussi PostgreSQL et Redis
  via apt (ports 5432 et 6379). Ici ils tournent en conteneurs sur 5433 et
  6380 : pas de conflit, mais garde-en un seul pour t'en servir.
- **Versions d'images** : `redis-commander:latest` est un tag flottant. Après
  vérification de la dernière version stable, décommente et renseigne
  `REDIS_COMMANDER_IMAGE` (et les autres) dans `/srv/docker/.env`, puis
  `docker compose --env-file .env up -d`.
- **Mise à jour majeure de PostgreSQL** (18 vers 19) : demande un
  `pg_upgrade` ou un dump/restauration, pas un simple changement de tag.
- **Sauvegardes sur la même machine** : elles protègent d'une erreur de
  manipulation, pas d'une panne de disque. Copie-les ailleurs (rsync, restic...).
