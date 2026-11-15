#!/bin/bash
# ==========================================================
# HOMEBOX BACKUP : PostgreSQL + Redis + .env
# Installé par infra-docker.sh dans /usr/local/sbin/homebox-backup
# Lancé chaque nuit par homebox-backup.timer (root).
# ==========================================================

set -Eeuo pipefail
umask 077

BASE_DIR="${BASE_DIR:-/srv/docker}"
ENV_FILE="${ENV_FILE:-$BASE_DIR/.env}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups/homebox}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

PG_CONTAINER="homebox-postgres"
REDIS_CONTAINER="homebox-redis"

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : lance ce script en root." >&2
    exit 1
fi

if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [ "$RETENTION_DAYS" -lt 1 ]; then
    echo "RETENTION_DAYS invalide : $RETENTION_DAYS" >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Fichier .env introuvable : $ENV_FILE" >&2
    exit 1
fi

# Lecture du .env par grep (jamais "source" : le fichier appartient
# à l'utilisateur, ce script s'exécute en root).
get_env() {
    grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2-
}

PG_USER="$(get_env POSTGRES_USER)"
PG_DB="$(get_env POSTGRES_DB)"

if [ -z "$PG_USER" ] || [ -z "$PG_DB" ]; then
    echo "POSTGRES_USER ou POSTGRES_DB manquant dans $ENV_FILE" >&2
    exit 1
fi

for container in "$PG_CONTAINER" "$REDIS_CONTAINER"; do
    state="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)"
    if [ "$state" != "running" ]; then
        echo "Conteneur non démarré : $container" >&2
        exit 1
    fi
done

install -d -m 0700 "$BACKUP_DIR"

# Un seul backup à la fois
exec 9> "$BACKUP_DIR/.lock"
if ! flock -n 9; then
    echo "Une sauvegarde est déjà en cours." >&2
    exit 1
fi

STAMP="$(date '+%Y%m%d-%H%M%S')"
WORK="$BACKUP_DIR/.partial-$STAMP"
FINAL="$BACKUP_DIR/$STAMP"

# Un dossier incomplet n'est jamais conservé
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK"

# ----------------------------------------------------------
# PostgreSQL (format custom, compressé)
# ----------------------------------------------------------

docker exec "$PG_CONTAINER" \
    pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$WORK/postgres.dump"

# Vérifie que le dump est lisible
docker exec -i "$PG_CONTAINER" pg_restore --list < "$WORK/postgres.dump" > /dev/null

# Rôles et paramètres globaux
docker exec "$PG_CONTAINER" \
    pg_dumpall -U "$PG_USER" --globals-only > "$WORK/postgres-globals.sql"

# ----------------------------------------------------------
# Redis (snapshot RDB ; le mot de passe reste dans le conteneur)
# ----------------------------------------------------------

docker exec "$REDIS_CONTAINER" sh -c \
    'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --rdb /tmp/backup.rdb' > /dev/null 2>&1
docker cp "$REDIS_CONTAINER:/tmp/backup.rdb" "$WORK/redis.rdb"
docker exec "$REDIS_CONTAINER" rm -f /tmp/backup.rdb

# ----------------------------------------------------------
# .env (sans lui, un volume Postgres existant est inutilisable)
# ----------------------------------------------------------

cp "$ENV_FILE" "$WORK/env.backup"

# ----------------------------------------------------------
# Contrôles d'intégrité
# ----------------------------------------------------------

for file in postgres.dump postgres-globals.sql redis.rdb env.backup; do
    if [ ! -s "$WORK/$file" ]; then
        echo "Fichier de sauvegarde vide : $file" >&2
        exit 1
    fi
done

( cd "$WORK" && sha256sum postgres.dump postgres-globals.sql redis.rdb env.backup > SHA256SUMS )

mv "$WORK" "$FINAL"

# ----------------------------------------------------------
# Rotation (uniquement après une sauvegarde réussie)
# ----------------------------------------------------------

find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '20[0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' \
    -mtime +"$RETENTION_DAYS" -exec rm -rf {} +

echo "Sauvegarde OK : $FINAL ($(du -sh "$FINAL" | cut -f1))"
echo "Sauvegardes conservées : $(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' | wc -l)"
