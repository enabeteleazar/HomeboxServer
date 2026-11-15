# shellcheck shell=bash
# ==========================================================
# HOMEBOX : fonctions communes à tous les outils homebox-*
# Installé dans /usr/local/lib/homebox/common.sh
# ==========================================================

HOMEBOX_CONF_DIR="${HOMEBOX_CONF_DIR:-/etc/homebox}"

# Valeurs par défaut (paths.conf, régénéré par infra-docker.sh, les remplace)
BASE_DIR="${BASE_DIR:-/srv/docker}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups/homebox}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STATE_DIR="${STATE_DIR:-/var/lib/homebox}"

PG_CONTAINER="${PG_CONTAINER:-homebox-postgres}"
REDIS_CONTAINER="${REDIS_CONTAINER:-homebox-redis}"
KUMA_CONTAINER="${KUMA_CONTAINER:-homebox-uptime-kuma}"

die() {
    echo "Erreur : $*" >&2
    exit 1
}

require_root() {
    [ "$EUID" -eq 0 ] || die "lance cette commande avec sudo."
}

# Source un fichier de configuration uniquement s'il appartient à root
# et n'est modifiable par personne d'autre (ces scripts tournent en root).
load_conf_file() {
    local file="$1"
    [ -f "$file" ] || return 0

    local owner mode
    owner="$(stat -c %u "$file")"
    mode="$(stat -c %a "$file")"

    if [ "$owner" != 0 ] || [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
        die "$file doit appartenir à root et n'être modifiable que par lui."
    fi

    set -a
    # shellcheck disable=SC1090
    . "$file"
    set +a
}

# Charge paths.conf (chemins, généré) puis homebox.conf (réglages, édité à la main)
load_conf() {
    load_conf_file "$HOMEBOX_CONF_DIR/paths.conf"
    load_conf_file "$HOMEBOX_CONF_DIR/homebox.conf"
    derive_paths
}

# ENV_FILE / COMPOSE_FILE suivent BASE_DIR, sauf s'ils ont été imposés
_FORCED_ENV_FILE="${ENV_FILE:-}"
_FORCED_COMPOSE_FILE="${COMPOSE_FILE:-}"

derive_paths() {
    ENV_FILE="${_FORCED_ENV_FILE:-$BASE_DIR/.env}"
    COMPOSE_FILE="${_FORCED_COMPOSE_FILE:-$BASE_DIR/compose.yml}"
}

derive_paths

# Lecture d'une variable du .env Docker par grep (jamais "source" :
# ce fichier appartient à l'utilisateur, les scripts tournent en root).
get_env() {
    [ -r "$ENV_FILE" ] || die "fichier .env illisible : $ENV_FILE"
    grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- || true
}

compose() {
    docker compose \
        --project-name homebox \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        "$@"
}

container_running() {
    [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || true)" = "running" ]
}

container_health() {
    docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "absent"
}

# Attend qu'un conteneur soit "healthy" (timeout en secondes)
wait_healthy() {
    local container="$1" timeout="${2:-120}" waited=0
    while [ "$(container_health "$container")" != "healthy" ]; do
        [ "$waited" -ge "$timeout" ] && return 1
        sleep 2
        waited=$(( waited + 2 ))
    done
}

# Dossier de sauvegarde : "latest" ou un horodatage ou un chemin
resolve_backup() {
    local wanted="${1:-latest}"

    if [ "$wanted" = "latest" ]; then
        wanted="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20[0-9]*-[0-9]*' -printf '%f\n' | sort | tail -n 1)"
        [ -n "$wanted" ] || die "aucune sauvegarde dans $BACKUP_DIR"
    fi

    if [ -d "$wanted" ]; then
        (cd "$wanted" && pwd)
    elif [ -d "$BACKUP_DIR/$wanted" ]; then
        echo "$BACKUP_DIR/$wanted"
    else
        die "sauvegarde introuvable : $wanted"
    fi
}

# Notification : journal système + ntfy si NTFY_URL est défini (homebox.conf)
# Usage : notify "Titre" "Message" [priorité 1-5] [tags]
notify() {
    local title="$1" message="$2" priority="${3:-3}" tags="${4:-}"
    local host
    host="$(hostname -s 2>/dev/null || echo homebox)"

    logger -t homebox -- "$title : $message" 2>/dev/null || true

    [ -n "${NTFY_URL:-}" ] || return 0

    local args=(
        -fsS -m 20 -o /dev/null
        --url-query "title=[$host] $title"
        --url-query "priority=$priority"
    )
    [ -n "$tags" ] && args+=(--url-query "tags=$tags")
    [ -n "${NTFY_TOKEN:-}" ] && args+=(-H "Authorization: Bearer $NTFY_TOKEN")

    curl "${args[@]}" --data-binary "$message" "$NTFY_URL" \
        || echo "Notification ntfy impossible ($NTFY_URL)." >&2
}

offsite_configured() {
    [ -f "$HOMEBOX_CONF_DIR/restic.env" ] &&
        grep -qE '^RESTIC_REPOSITORY=.+' "$HOMEBOX_CONF_DIR/restic.env"
}
