#!/bin/bash
# ==========================================================
# HOMEBOX DOCKER INFRASTRUCTURE
# Ubuntu 26.04 LTS
# Usage : sudo bash ./infra-docker.sh
#         sudo bash ./infra-docker.sh --tools-only   (outils, sudoers et tâches
#                                                     planifiées seulement, sans
#                                                     toucher aux conteneurs)
# (lancer depuis le dossier du projet : il utilise ./files/)
# Relançable : sert aussi à mettre à jour une installation existante.
# ==========================================================

set -Eeuo pipefail

# ==========================================================
# CONFIGURATION
# ==========================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOOLS_ONLY=false
case "${1:-}" in
    --tools-only) TOOLS_ONLY=true ;;
    "") ;;
    *) echo "Option inconnue : $1 (seule option : --tools-only)"; exit 1 ;;
esac
FILES_DIR="$SCRIPT_DIR/files"

BASE_DIR="${BASE_DIR:-/srv/docker}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups/homebox}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

COMPOSE_FILE="$BASE_DIR/compose.yml"
ENV_FILE="$BASE_DIR/.env"
COMPOSE_HASH_FILE="$BASE_DIR/.compose.sha256"

CONF_DIR="/etc/homebox"
STATE_DIR="/var/lib/homebox"
LIB_DIR="/usr/local/lib/homebox"
SBIN_DIR="/usr/local/sbin"
BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"
CADDY_DIR="/etc/caddy"

# Empreintes des compose.yml livrés par les versions précédentes du kit :
# un fichier identique à l'une d'elles n'a pas été modifié à la main
# et peut être remplacé sans risque.
KNOWN_COMPOSE_HASHES=(
    27cf45c565022ec4a2896c7653e7362313cb74c7d8ccfec2fe3f5ac5b50649b4
)

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

for required in \
    "$FILES_DIR/compose.yml" \
    "$FILES_DIR/lib/common.sh" \
    "$FILES_DIR/bin/homebox-backup" \
    "$FILES_DIR/config/homebox.conf" \
    "$FILES_DIR/config/restic.env.example" \
    "$FILES_DIR/caddy/Caddyfile"
do
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

env_value() {
    grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2-
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

    # Les sauvegardes et la configuration contiennent des secrets : root uniquement
    install -d -o root -g root -m 0700 "$BACKUP_DIR"
    install -d -o root -g root -m 0700 "$CONF_DIR"
    install -d -o root -g root -m 0755 "$STATE_DIR" "$LIB_DIR"
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
UPTIME_KUMA_PORT=3001

# Optionnel : forcer une autre version d'image (voir README)
# POSTGRES_IMAGE=postgres:18
# REDIS_IMAGE=redis:8-alpine
# ADMINER_IMAGE=adminer:5
# REDIS_COMMANDER_IMAGE=ghcr.io/joeferner/redis-commander:0.9
# UPTIME_KUMA_IMAGE=louislam/uptime-kuma:2
ENV
        )

        notice "Nouveau .env généré : $ENV_FILE (à sauvegarder hors de la machine)."

    else

        notice "Fichier .env existant conservé : identifiants inchangés."

        # Variables ajoutées par les versions récentes du kit
        if ! grep -q '^UPTIME_KUMA_PORT=' "$ENV_FILE"; then
            printf '\n# Ajouté par infra-docker.sh\nUPTIME_KUMA_PORT=3001\n' >> "$ENV_FILE"
            notice "UPTIME_KUMA_PORT=3001 ajouté au .env."
        fi

        # L'ancienne image Redis Commander (Docker Hub) n'est plus maintenue
        if grep -q '^REDIS_COMMANDER_IMAGE=rediscommander/' "$ENV_FILE"; then
            sed -i 's|^REDIS_COMMANDER_IMAGE=rediscommander/.*|REDIS_COMMANDER_IMAGE=ghcr.io/joeferner/redis-commander:0.9|' "$ENV_FILE"
            notice "Image Redis Commander remplacée par la version maintenue (ghcr.io)."
        fi
        sed -i 's|^# REDIS_COMMANDER_IMAGE=rediscommander/redis-commander:latest|# REDIS_COMMANDER_IMAGE=ghcr.io/joeferner/redis-commander:0.9|' "$ENV_FILE"

        local variable

        for variable in \
            POSTGRES_USER \
            POSTGRES_DB \
            POSTGRES_PASSWORD \
            POSTGRES_PORT \
            REDIS_PASSWORD \
            REDIS_PORT \
            ADMINER_PORT \
            REDIS_COMMANDER_PORT \
            UPTIME_KUMA_PORT
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
            REDIS_COMMANDER_PORT \
            UPTIME_KUMA_PORT
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
# COMPOSE FILE (jamais écrasé s'il a été modifié à la main)
# ==========================================================

install_compose_file() {

    local new_hash current_hash recorded_hash="" known
    new_hash="$(sha256sum "$FILES_DIR/compose.yml" | cut -d' ' -f1)"

    if [ ! -f "$COMPOSE_FILE" ]; then
        install -o "$TARGET_USER" -g "$USER_GROUP" -m 0644 "$FILES_DIR/compose.yml" "$COMPOSE_FILE"
        echo "$new_hash" > "$COMPOSE_HASH_FILE"
        return 0
    fi

    current_hash="$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)"

    if [ "$current_hash" = "$new_hash" ]; then
        echo "compose.yml déjà à jour."
        echo "$new_hash" > "$COMPOSE_HASH_FILE"
        return 0
    fi

    [ -f "$COMPOSE_HASH_FILE" ] && recorded_hash="$(cat "$COMPOSE_HASH_FILE")"

    known=false
    [ "$current_hash" = "$recorded_hash" ] && known=true
    for hash in "${KNOWN_COMPOSE_HASHES[@]}"; do
        [ "$current_hash" = "$hash" ] && known=true
    done

    if $known; then
        # Fichier d'origine, jamais retouché : mise à jour sans risque
        local saved
        saved="$COMPOSE_FILE.bak-$(date '+%Y%m%d-%H%M%S')"
        cp -p "$COMPOSE_FILE" "$saved"
        install -o "$TARGET_USER" -g "$USER_GROUP" -m 0644 "$FILES_DIR/compose.yml" "$COMPOSE_FILE"
        echo "$new_hash" > "$COMPOSE_HASH_FILE"
        notice "compose.yml mis à jour (ancienne version : $saved)."
    else
        install -o "$TARGET_USER" -g "$USER_GROUP" -m 0644 "$FILES_DIR/compose.yml" "$COMPOSE_FILE.new"
        notice "compose.yml modifié à la main : conservé tel quel."
        notice "Nouvelle version proposée dans ${COMPOSE_FILE}.new (compare avec diff)."
    fi

    chown "$TARGET_USER:$USER_GROUP" "$COMPOSE_HASH_FILE"
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
        homebox-redis-commander \
        homebox-uptime-kuma
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
# OUTILS HOMEBOX (scripts, configuration, restic)
# ==========================================================

install_tools() {

    # Installés hors de $BASE_DIR : ce dossier appartient à l'utilisateur,
    # un script exécuté par root ne doit pas y être modifiable.
    install -o root -g root -m 0644 "$FILES_DIR/lib/common.sh" "$LIB_DIR/common.sh"

    local tool
    for tool in backup restore healthcheck notify offsite update site firewall smart status; do
        install -o root -g root -m 0750 "$FILES_DIR/bin/homebox-$tool" "$SBIN_DIR/homebox-$tool"
    done

    # Utilisable sans sudo (membres du groupe docker)
    install -o root -g root -m 0755 "$FILES_DIR/bin/homebox-project" "$BIN_DIR/homebox-project"

    # Politique pare-feu, appelée par homebox-firewall (page Firewall du dashboard)
    install -o root -g root -m 0644 "$SCRIPT_DIR/firewall.sh" "$LIB_DIR/firewall.sh"

    # Le dashboard (qui tourne sous $TARGET_USER) pilote UFW via homebox-firewall,
    # et uniquement lui : ce script valide lui-même tous ses arguments.
    local sudoers_tmp
    sudoers_tmp="$(mktemp)"
    cat > "$sudoers_tmp" <<SUDOERS
# HOMEBOX : géré par infra-docker.sh
# Le dashboard peut : piloter le pare-feu via homebox-firewall (qui valide tout),
# lancer une sauvegarde et rafraîchir l'état du serveur. Rien d'autre.
$TARGET_USER ALL=(root) NOPASSWD: $SBIN_DIR/homebox-firewall
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block homebox-backup.service
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block homebox-healthcheck.service
SUDOERS
    if visudo -cf "$sudoers_tmp" >/dev/null; then
        install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/homebox-firewall
    else
        echo "Règle sudoers invalide : non installée."
        rm -f "$sudoers_tmp"
        return 1
    fi
    rm -f "$sudoers_tmp"

    # Chemins partagés par tous les outils (réécrit à chaque exécution)
    cat > "$CONF_DIR/paths.conf" <<CONF
# Généré par infra-docker.sh à chaque exécution : ne pas modifier ici.
BASE_DIR="$BASE_DIR"
BACKUP_DIR="$BACKUP_DIR"
RETENTION_DAYS="$RETENTION_DAYS"
STATE_DIR="$STATE_DIR"
HOMEBOX_USER="$TARGET_USER"
CONF
    chmod 600 "$CONF_DIR/paths.conf"

    # Réglages modifiables : jamais écrasés
    if [ ! -f "$CONF_DIR/homebox.conf" ]; then
        local topic
        topic="homebox-$(openssl rand -hex 12)"
        sed "s|__NTFY_URL__|https://ntfy.sh/$topic|" "$FILES_DIR/config/homebox.conf" > "$CONF_DIR/homebox.conf"
        chmod 600 "$CONF_DIR/homebox.conf"
        notice "Notifications : installe l'app ntfy et abonne-toi au sujet $topic"
        notice "  (réglages : sudo nano $CONF_DIR/homebox.conf, test : sudo homebox-notify --test)"
    fi

    if [ ! -f "$CONF_DIR/restic.env" ]; then
        install -o root -g root -m 0600 "$FILES_DIR/config/restic.env.example" "$CONF_DIR/restic.env"
    fi
    install -o root -g root -m 0600 "$FILES_DIR/config/restic.env.example" "$CONF_DIR/restic.env.example"

    if [ -z "$(HOMEBOX_CONF_DIR="$CONF_DIR" bash -c '. "$1"; restic_repository_value' _ "$LIB_DIR/common.sh")" ]; then
        notice "Copie externe à configurer : sudo nano $CONF_DIR/restic.env puis sudo homebox-offsite init"
    fi

    if ! command -v restic >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y restic
    fi

    # Santé des disques (homebox-smart). --no-install-recommends : pas de
    # serveur de mail tiré par smartmontools, les alertes passent par ntfy.
    if ! command -v smartctl >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends smartmontools
    fi
}

# ==========================================================
# SYSTEMD (sauvegardes, santé, test de restauration, alertes)
# ==========================================================

install_systemd() {

    local unit
    for unit in "$FILES_DIR"/systemd/*; do
        install -o root -g root -m 0644 "$unit" "$SYSTEMD_DIR/$(basename "$unit")"
    done

    systemctl daemon-reload
    systemctl enable --now \
        homebox-backup.timer \
        homebox-healthcheck.timer \
        homebox-restore-test.timer \
        homebox-smart.timer

    # Premier relevé SMART tout de suite (sans auto-test), pour le dashboard
    "$SBIN_DIR/homebox-smart" --no-test || true
}

# ==========================================================
# CADDY (un fichier par site dans /etc/caddy/sites/)
# ==========================================================

configure_caddy() {

    if ! command -v caddy >/dev/null 2>&1; then
        echo "Caddy non installé : étape ignorée."
        return 0
    fi

    install -d -o root -g root -m 0755 "$CADDY_DIR/sites"

    local caddyfile="$CADDY_DIR/Caddyfile"

    if [ -f "$caddyfile" ] && cmp -s "$FILES_DIR/caddy/Caddyfile" "$caddyfile"; then
        echo "Caddyfile déjà à jour."
        return 0
    fi

    # Remplacé seulement s'il s'agit du fichier d'exemple fourni par le paquet
    if [ ! -f "$caddyfile" ] || grep -q 'The Caddyfile is an easy way to configure' "$caddyfile"; then
        [ -f "$caddyfile" ] && cp -p "$caddyfile" "$caddyfile.orig"
        install -o root -g root -m 0644 "$FILES_DIR/caddy/Caddyfile" "$caddyfile"

        if ! caddy validate --config "$caddyfile" --adapter caddyfile; then
            [ -f "$caddyfile.orig" ] && mv "$caddyfile.orig" "$caddyfile"
            echo "Caddyfile refusé, ancienne version restaurée."
            return 1
        fi

        systemctl reload caddy
        notice "Caddy configuré : ajoute un site avec « sudo homebox-site add <domaine> <port> »."
    else
        install -o root -g root -m 0644 "$FILES_DIR/caddy/Caddyfile" "$caddyfile.new"
        notice "Caddyfile personnalisé conservé ; modèle HOMEBOX dans $caddyfile.new"
    fi
}

# ==========================================================
# VERIFICATIONS FINALES
# ==========================================================

run_test_backup() {
    "$SBIN_DIR/homebox-backup"
}

run_test_restore() {
    "$SBIN_DIR/homebox-restore" test latest
}

check_docker_defaults() {
    # Docker contourne UFW : un port publié sans adresse est ouvert sur Internet.
    # "ip": "127.0.0.1" change le comportement par défaut (install-node.sh le règle).
    if ! grep -qE '"ip"[[:space:]]*:[[:space:]]*"127\.0\.0\.1"' /etc/docker/daemon.json 2>/dev/null; then
        notice "Docker publie les ports sur toutes les interfaces par défaut : relance install-node.sh"
        notice "  ou ajoute \"ip\": \"127.0.0.1\" dans /etc/docker/daemon.json"
    fi
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
    echo "========== TÂCHES PLANIFIÉES =========="
    systemctl list-timers 'homebox-*' --no-pager || true
    echo ""

    echo "========== SANTÉ =========="
    "$SBIN_DIR/homebox-healthcheck" --no-notify || true
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

if $TOOLS_ONLY; then
    run_step "Création des dossiers HOMEBOX" \
        create_directories
    run_step "Installation des outils HOMEBOX" \
        install_tools
    run_step "Installation des tâches planifiées" \
        install_systemd
    echo ""
    echo -e "${GREEN}✔ Outils HOMEBOX à jour (conteneurs inchangés).${RESET}"
    echo "Log : $PERSIST_LOG"
    exit 0
fi

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

run_step "Installation des outils HOMEBOX" \
    install_tools

run_step "Installation des tâches planifiées" \
    install_systemd

run_step "Configuration de Caddy" \
    configure_caddy

run_step "Sauvegarde de test" \
    run_test_backup

run_step "Test de restauration à blanc" \
    run_test_restore

check_docker_defaults

# ==========================================================
# RESULTAT
# ==========================================================

echo ""
echo -e "${GREEN}Installation terminée !${RESET}"
echo ""

show_status

echo "========== ACCES LOCAUX =========="
echo "PostgreSQL       : localhost:$(env_value POSTGRES_PORT)"
echo "Redis            : localhost:$(env_value REDIS_PORT)"
echo "Adminer          : http://localhost:$(env_value ADMINER_PORT)"
echo "Redis Commander  : http://localhost:$(env_value REDIS_COMMANDER_PORT)"
echo "Uptime Kuma      : http://localhost:$(env_value UPTIME_KUMA_PORT)"
echo "=================================="
echo ""

echo "Dossier Docker : $BASE_DIR"
echo "Sauvegardes    : $BACKUP_DIR (quotidien 03:30, ${RETENTION_DAYS} jours)"
echo "Identifiants   : $ENV_FILE"
echo "Réglages       : $CONF_DIR/homebox.conf"
echo "Log            : $PERSIST_LOG"
echo ""

if [ -s "$NOTICE_FILE" ]; then
    echo -e "${YELLOW}Infos :${RESET}"
    while IFS= read -r line; do
        echo -e "${YELLOW}  - ${line}${RESET}"
    done < "$NOTICE_FILE"
    echo ""
fi

echo -e "${YELLOW}Accès distant : ssh -L $(env_value ADMINER_PORT):localhost:$(env_value ADMINER_PORT) -L $(env_value REDIS_COMMANDER_PORT):localhost:$(env_value REDIS_COMMANDER_PORT) -L $(env_value UPTIME_KUMA_PORT):localhost:$(env_value UPTIME_KUMA_PORT) ${TARGET_USER}@homebox${RESET}"
echo -e "${YELLOW}Ne partage jamais le fichier .env, et configure la copie externe des sauvegardes.${RESET}"
echo ""
echo -e "${GREEN}✔ HOMEBOX DOCKER INFRASTRUCTURE PRÊTE !${RESET}"
echo ""
