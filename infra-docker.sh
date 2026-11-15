#!/bin/bash
# ==========================================================
# HOMEBOX DOCKER INFRASTRUCTURE
# Ubuntu 26.04 LTS
# Usage : sudo bash ./infra-docker.sh
# (lancer depuis le dossier du projet : il utilise ./files/)
# ==========================================================

set -Eeuo pipefail

# ==========================================================
# CONFIGURATION
# ==========================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"

BASE_DIR="${BASE_DIR:-/srv/docker}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups/homebox}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

COMPOSE_FILE="$BASE_DIR/compose.yml"
ENV_FILE="$BASE_DIR/.env"

BACKUP_BIN="/usr/local/sbin/homebox-backup"

LOG_FILE="$(mktemp /tmp/homebox-docker.XXXXXX)"
NOTICE_FILE="$(mktemp /tmp/homebox-notices.XXXXXX)"
PERSIST_LOG="/var/log/homebox-docker-$(date '+%Y%m%d-%H%M%S').log"

# Nettoyage garanti, même si une vérification échoue plus bas
trap 'rm -f "$LOG_FILE" "$NOTICE_FILE"' EXIT

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
RESET='\033[0m'

# ==========================================================
# VERIFICATIONS
# ==========================================================

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : utilise sudo."
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"

if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "Erreur : utilisateur cible introuvable (lance avec sudo depuis un utilisateur normal)."
    exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "Utilisateur inexistant."
    exit 1
fi

USER_GROUP="$(id -gn "$TARGET_USER")"

if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [ "$RETENTION_DAYS" -lt 1 ]; then
    echo "RETENTION_DAYS doit être un entier >= 1."
    exit 1
fi

for required in "$FILES_DIR/compose.yml" "$FILES_DIR/backup.sh"; do
    if [ ! -f "$required" ]; then
        echo "Fichier manquant : $required"
        echo "Lance le script depuis le dossier complet du projet."
        exit 1
    fi
done

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker n'est pas installé."
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose n'est pas disponible."
    exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "OpenSSL est nécessaire."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Le service Docker ne fonctionne pas."
    exit 1
fi

install -m 0600 /dev/null "$PERSIST_LOG"

# ==========================================================
# ANIMATION ET LOGS
# ==========================================================

run_step() {

    local message="$1"
    shift

    "$@" > "$LOG_FILE" 2>&1 &
    local pid=$!

    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${BLUE}%s${RESET} %s..." "${frames[$i]}" "$message"
        i=$(( (i + 1) % ${#frames[@]} ))
        sleep 0.1
    done

    local status=0
    wait "$pid" || status=$?

    {
        echo "===== $(date '+%F %T') | $message | code=$status ====="
        cat "$LOG_FILE"
        echo ""
    } >> "$PERSIST_LOG"

    if [ "$status" -eq 0 ]; then
        printf "\r${GREEN}✔${RESET} %s terminé.                 \n" "$message"
    else
        printf "\r${RED}✘${RESET} Échec : %s.\n" "$message"
        echo ""
        cat "$LOG_FILE"
        echo ""
        echo "Log complet : $PERSIST_LOG"
        exit 1
    fi
}

# Message à afficher à la fin (run_step masque la sortie des étapes)
notice() {
    echo "$*" >> "$NOTICE_FILE"
}

# ==========================================================
# DOCKER COMPOSE
# ==========================================================

compose() {

    docker compose \
        --project-name homebox \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        "$@"
}

# ==========================================================
# DOSSIERS
# ==========================================================

create_directories() {

    install -d -o "$TARGET_USER" -g "$USER_GROUP" -m 0755 "$BASE_DIR"

    # Les sauvegardes contiennent des secrets : root uniquement
    install -d -o root -g root -m 0700 "$BACKUP_DIR"
}

# ==========================================================
# IDENTIFIANTS
# ==========================================================

create_environment() {

    if [ ! -f "$ENV_FILE" ]; then

        local pg_password
        local redis_password

        pg_password="$(openssl rand -hex 32)"
        redis_password="$(openssl rand -hex 32)"

        (
            umask 077
            cat > "$ENV_FILE" <<ENV
# HOMEBOX DATABASE CONFIGURATION
# ATTENTION : POSTGRES_PASSWORD n'est appliqué qu'à la première
# initialisation du volume. Sauvegarde ce fichier avec les dumps.

POSTGRES_USER=homebox
POSTGRES_DB=homebox
POSTGRES_PASSWORD=${pg_password}
POSTGRES_PORT=5433

REDIS_PASSWORD=${redis_password}
REDIS_PORT=6380

ADMINER_PORT=8080
REDIS_COMMANDER_PORT=8081

# Optionnel : fixer les versions des images (voir README)
# POSTGRES_IMAGE=postgres:18
# REDIS_IMAGE=redis:8-alpine
# ADMINER_IMAGE=adminer:5
# REDIS_COMMANDER_IMAGE=rediscommander/redis-commander:latest
ENV
        )

        notice "Nouveau .env généré : $ENV_FILE (à sauvegarder hors de la machine)."

    else

        notice "Fichier .env existant conservé : identifiants inchangés."

        local variable

        for variable in \
            POSTGRES_USER \
            POSTGRES_DB \
            POSTGRES_PASSWORD \
            POSTGRES_PORT \
            REDIS_PASSWORD \
            REDIS_PORT \
            ADMINER_PORT \
            REDIS_COMMANDER_PORT
        do
            if ! grep -q "^${variable}=." "$ENV_FILE"; then
                echo "Variable manquante ou vide dans .env : $variable"
                return 1
            fi
        done

        for variable in \
            POSTGRES_PORT \
            REDIS_PORT \
            ADMINER_PORT \
            REDIS_COMMANDER_PORT
        do
            if ! grep -qE "^${variable}=[0-9]{2,5}$" "$ENV_FILE"; then
                echo "Port invalide dans .env : $variable"
                return 1
            fi
        done

    fi

    chown "$TARGET_USER:$USER_GROUP" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# ==========================================================
# COMPOSE FILE (jamais écrasé s'il a été modifié)
# ==========================================================

install_compose_file() {

    if [ ! -f "$COMPOSE_FILE" ]; then

        install -o "$TARGET_USER" -g "$USER_GROUP" -m 0644 \
            "$FILES_DIR/compose.yml" "$COMPOSE_FILE"

    elif cmp -s "$FILES_DIR/compose.yml" "$COMPOSE_FILE"; then

        echo "compose.yml déjà à jour."

    else

        install -o "$TARGET_USER" -g "$USER_GROUP" -m 0644 \
            "$FILES_DIR/compose.yml" "$COMPOSE_FILE.new"

        notice "compose.yml existant différent : conservé tel quel."
        notice "Nouvelle version proposée dans ${COMPOSE_FILE}.new (compare avec diff)."

    fi
}

# ==========================================================
# VALIDATION / IMAGES / DEMARRAGE
# ==========================================================

validate_compose() {
    compose config -q
}

pull_images() {
    compose pull
}

start_services() {
    compose up -d --remove-orphans --wait --wait-timeout 180
}

# ==========================================================
# VERIFICATION
# ==========================================================

check_services() {

    local service
    local status
    local health

    for service in \
        homebox-postgres \
        homebox-redis \
        homebox-adminer \
        homebox-redis-commander
    do
        status="$(docker inspect -f '{{.State.Status}}' "$service" 2>/dev/null || true)"

        if [ "$status" != "running" ]; then
            echo "Service non démarré : $service"
            return 1
        fi
    done

    for service in homebox-postgres homebox-redis; do

        health="$(docker inspect \
            -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$service")"

        if [ "$health" != "healthy" ]; then
            echo "Service non healthy : $service ($health)"
            return 1
        fi
    done
}

# ==========================================================
# SAUVEGARDES (script root + timer systemd)
# ==========================================================

install_backup() {

    # Installé hors de $BASE_DIR : ce dossier appartient à l'utilisateur,
    # un script exécuté par root ne doit pas y être modifiable.
    install -o root -g root -m 0750 "$FILES_DIR/backup.sh" "$BACKUP_BIN"

    cat > /etc/systemd/system/homebox-backup.service <<UNIT
[Unit]
Description=Sauvegarde HOMEBOX (PostgreSQL + Redis)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
Environment=BASE_DIR=${BASE_DIR}
Environment=BACKUP_DIR=${BACKUP_DIR}
Environment=RETENTION_DAYS=${RETENTION_DAYS}
ExecStart=${BACKUP_BIN}
UNIT

    cat > /etc/systemd/system/homebox-backup.timer <<UNIT
[Unit]
Description=Sauvegarde quotidienne HOMEBOX

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

    systemctl daemon-reload
    systemctl enable --now homebox-backup.timer
}

run_test_backup() {

    env \
        BASE_DIR="$BASE_DIR" \
        BACKUP_DIR="$BACKUP_DIR" \
        RETENTION_DAYS="$RETENTION_DAYS" \
        "$BACKUP_BIN"
}

# ==========================================================
# AFFICHAGE
# ==========================================================

show_status() {

    echo ""
    echo "========== CONTENEURS =========="
    compose ps

    echo ""
    echo "========== STOCKAGE =========="
    docker volume ls --filter name=homebox

    echo ""
    echo "========== SAUVEGARDES =========="
    systemctl list-timers homebox-backup.timer --no-pager || true
    echo ""
}

# ==========================================================
# BANNIERE
# ==========================================================

echo ""
echo -e "${BLUE}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║       HOMEBOX DOCKER INFRASTRUCTURE  ║"
echo "  ║             DOCKER COMPOSE           ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${RESET}"
echo ""

# ==========================================================
# INSTALLATION
# ==========================================================

run_step "Création des dossiers HOMEBOX" \
    create_directories

run_step "Génération des identifiants sécurisés" \
    create_environment

run_step "Installation du Docker Compose" \
    install_compose_file

run_step "Validation de la configuration" \
    validate_compose

run_step "Téléchargement des images Docker" \
    pull_images

run_step "Démarrage des services" \
    start_services

run_step "Vérification des conteneurs" \
    check_services

run_step "Installation des sauvegardes automatiques" \
    install_backup

run_step "Sauvegarde de test" \
    run_test_backup

# ==========================================================
# RESULTAT
# ==========================================================

echo ""
echo -e "${GREEN}Installation terminée !${RESET}"
echo ""

show_status

echo "========== ACCES LOCAUX =========="
echo "PostgreSQL       : localhost:5433"
echo "Redis            : localhost:6380"
echo "Adminer          : http://localhost:8080"
echo "Redis Commander  : http://localhost:8081"
echo "=================================="
echo ""

echo "Dossier Docker : $BASE_DIR"
echo "Sauvegardes    : $BACKUP_DIR (quotidien 03:30, ${RETENTION_DAYS} jours)"
echo "Identifiants   : $ENV_FILE"
echo "Log            : $PERSIST_LOG"
echo ""

if [ -s "$NOTICE_FILE" ]; then
    echo -e "${YELLOW}Infos :${RESET}"
    while IFS= read -r line; do
        echo -e "${YELLOW}  - ${line}${RESET}"
    done < "$NOTICE_FILE"
    echo ""
fi

echo -e "${YELLOW}Accès distant : ssh -L 8080:localhost:8080 -L 8081:localhost:8081 ${TARGET_USER}@homebox${RESET}"
echo -e "${YELLOW}Ne partage jamais le fichier .env, et copie les sauvegardes hors de cette machine.${RESET}"
echo ""
echo -e "${GREEN}✔ HOMEBOX DOCKER INFRASTRUCTURE PRÊTE !${RESET}"
echo ""
