#!/bin/bash
# ==========================================================
# HOMEBOX DEV ENVIRONMENT v2
# Ubuntu 26.04 LTS
# Usage : sudo bash ./install-node.sh
# ==========================================================

set -Eeuo pipefail
clear || true

# ==========================================================
# OPTIONS
# ==========================================================

OPEN_WEB="${OPEN_WEB:-true}"
SSH_TAILSCALE_ONLY="${SSH_TAILSCALE_ONLY:-false}"
SSH_PORT="${SSH_PORT:-22}"
ADD_USER_TO_DOCKER_GROUP="${ADD_USER_TO_DOCKER_GROUP:-true}"
# PostgreSQL et Redis tournent dans Docker (infra-docker.sh) :
# la version système n'est installée que sur demande.
INSTALL_SYSTEM_DB="${INSTALL_SYSTEM_DB:-false}"
# Durcissement SSH (clés uniquement) si une clé est déjà autorisée
HARDEN_SSH="${HARDEN_SSH:-true}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ==========================================================
# ENVIRONNEMENT
# ==========================================================

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
RESET='\033[0m'

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : lance le script avec sudo."
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"

if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "Erreur : utilisateur cible introuvable."
    exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "Erreur : utilisateur inexistant."
    exit 1
fi

USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
USER_GROUP="$(id -gn "$TARGET_USER")"

ARCH="$(dpkg --print-architecture)"
CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"

PNPM_HOME="$USER_HOME/.local/share/pnpm"
PNPM_BIN="$PNPM_HOME/bin"
BASHRC="$USER_HOME/.bashrc"

if [ -z "$USER_HOME" ] || [ ! -d "$USER_HOME" ]; then
    echo "Erreur : dossier utilisateur introuvable."
    exit 1
fi

if [ -f /etc/os-release ]; then
    . /etc/os-release
else
    echo "Erreur : distribution inconnue."
    exit 1
fi

if [ "$ID" != "ubuntu" ] || [ "$VERSION_ID" != "26.04" ]; then
    echo "Erreur : ce script nécessite Ubuntu 26.04."
    exit 1
fi

if [[ ! "$SSH_PORT" =~ ^[0-9]+$ ]] ||
   [ "$SSH_PORT" -lt 1 ] ||
   [ "$SSH_PORT" -gt 65535 ]; then
    echo "Erreur : port SSH invalide."
    exit 1
fi

if [[ "$OPEN_WEB" != "true" && "$OPEN_WEB" != "false" ]]; then
    echo "Erreur : OPEN_WEB doit être true ou false."
    exit 1
fi

if [[ "$SSH_TAILSCALE_ONLY" != "true" && "$SSH_TAILSCALE_ONLY" != "false" ]]; then
    echo "Erreur : SSH_TAILSCALE_ONLY doit être true ou false."
    exit 1
fi

for option in INSTALL_SYSTEM_DB HARDEN_SSH; do
    if [[ "${!option}" != "true" && "${!option}" != "false" ]]; then
        echo "Erreur : $option doit être true ou false."
        exit 1
    fi
done

# ==========================================================
# LOGS
# ==========================================================

LOG_FILE="$(mktemp /tmp/homebox-install.XXXXXX)"
PERSIST_LOG="/var/log/homebox-install-$(date '+%Y%m%d-%H%M%S').log"

install -m 0600 /dev/null "$PERSIST_LOG"

trap 'rm -f "$LOG_FILE"' EXIT

# ==========================================================
# ANIMATION
# ==========================================================

run_step() {
    local message="$1"
    shift

    "$@" > "$LOG_FILE" 2>&1 &
    local pid=$!

    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${BLUE}%s${RESET} %s..." \
            "${frames[$i]}" "$message"

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

# ==========================================================
# GITHUB CLI
# ==========================================================

setup_github_cli_repo() {

    install -d -m 0755 /usr/share/keyrings

    curl -fsSL \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg

    chmod 644 /usr/share/keyrings/githubcli-archive-keyring.gpg

    rm -f /etc/apt/sources.list.d/github-cli.sources

    echo "deb [arch=$ARCH signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list

    apt-get update
}

# ==========================================================
# DOCKER
# ==========================================================

setup_docker_repo() {

    if [[ "$ARCH" != "amd64" && "$ARCH" != "arm64" ]]; then
        echo "Architecture Docker non prise en charge : $ARCH"
        return 1
    fi

    install -m 0755 -d /etc/apt/keyrings

    curl -fsSL \
        https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc

    chmod a+r /etc/apt/keyrings/docker.asc

    rm -f /etc/apt/sources.list.d/docker.sources

    local suite="$CODENAME"

    # Pas de fallback vers une autre distribution.
    if ! curl -fsSL \
        "https://download.docker.com/linux/ubuntu/dists/${suite}/Release" \
        -o /dev/null; then

        echo "Le dépôt Docker ne propose pas Ubuntu ${suite}."
        echo "Installation interrompue pour éviter un mélange de distributions."
        return 1
    fi

    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${suite} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update
}

# Réglages par défaut du démon Docker (fusionnés avec l'existant) :
# - ports publiés sur 127.0.0.1 par défaut : Docker contourne UFW,
#   un « -p 5432:5432 » serait sinon ouvert sur Internet ;
# - rotation des logs pour tous les conteneurs.
configure_docker_daemon() {

    local file=/etc/docker/daemon.json
    install -d -m 0755 /etc/docker
    [ -s "$file" ] || echo '{}' > "$file"

    local before
    before="$(sha256sum "$file")"

    python3 - "$file" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    conf = json.load(f)
conf.setdefault("ip", "127.0.0.1")
if "log-driver" not in conf:
    conf["log-driver"] = "json-file"
    conf.setdefault("log-opts", {"max-size": "10m", "max-file": "3"})
with open(path, "w") as f:
    json.dump(conf, f, indent=2)
    f.write("\n")
PY

    if [ "$(sha256sum "$file")" != "$before" ]; then
        systemctl restart docker
    fi
}

# ==========================================================
# NODE.JS
# ==========================================================

setup_nodesource() {

    local tmp
    tmp="$(mktemp)"

    if ! curl -fsSL \
        https://deb.nodesource.com/setup_24.x \
        -o "$tmp"; then

        rm -f "$tmp"
        return 1
    fi

    if bash "$tmp"; then
        rm -f "$tmp"
    else
        local status=$?
        rm -f "$tmp"
        return "$status"
    fi
}

# ==========================================================
# PNPM
# ==========================================================

prepare_pnpm_dir() {

    install -d \
        -o "$TARGET_USER" \
        -g "$USER_GROUP" \
        "$PNPM_HOME" "$PNPM_BIN"
}

pnpm_as_user() {

    runuser -u "$TARGET_USER" -- env \
        HOME="$USER_HOME" \
        PNPM_HOME="$PNPM_HOME" \
        PATH="$PNPM_BIN:/usr/local/bin:/usr/bin:/bin" \
        pnpm "$@"
}

# ==========================================================
# CADDY
# ==========================================================

setup_caddy_repo() {

    install -d -m 0755 /usr/share/keyrings

    rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    rm -f /etc/apt/sources.list.d/caddy-stable.list
    rm -f /etc/apt/sources.list.d/caddy-stable.sources

    curl -1sLf \
        https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor \
        -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg

    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg

    curl -1sLf \
        https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        -o /etc/apt/sources.list.d/caddy-stable.list

    chmod o+r /etc/apt/sources.list.d/caddy-stable.list

    apt-get update
}

# ==========================================================
# SECURITE UFW
# ==========================================================

configure_firewall() {

    local ufw_status
    ufw_status="$(ufw status)"

    local active=false

    if echo "$ufw_status" | grep -q "Status: active"; then
        active=true
    fi

    local client_ip=""
    local current_server_port=""

    if [ -n "${SSH_CONNECTION:-}" ]; then
        read -r client_ip _ _ current_server_port <<< "$SSH_CONNECTION"

        if [ "$current_server_port" != "$SSH_PORT" ]; then
            echo "Le port configuré ($SSH_PORT) ne correspond pas"
            echo "au port de la session SSH actuelle ($current_server_port)."
            return 1
        fi
    elif [ "$active" = false ]; then
        echo "Connexion SSH impossible à vérifier."
        echo "UFW ne sera pas activé pour éviter de perdre l'accès."
        echo "Relance depuis une session SSH avec SSH_CONNECTION préservé."
        return 1
    fi

    # Mode Tailscale exclusif
    if [ "$SSH_TAILSCALE_ONLY" = "true" ]; then

        if [ -z "$client_ip" ]; then
            echo "Impossible de vérifier la source SSH."
            return 1
        fi

        if ! ip link show tailscale0 >/dev/null 2>&1; then
            echo "Interface tailscale0 introuvable."
            return 1
        fi

        if ! ip route get "$client_ip" | grep -q 'dev tailscale0'; then
            echo "La session SSH actuelle ne passe pas par Tailscale."
            echo "Refus de modifier les règles SSH."
            return 1
        fi

        # Refuser de prétendre restreindre SSH si une ancienne
        # règle autorise déjà le port depuis toutes les interfaces.
        if ufw status | grep -Eq \
            "^${SSH_PORT}/tcp[[:space:]]+(ALLOW|LIMIT)[[:space:]]+IN[[:space:]]+Anywhere"; then

            echo "Une ancienne règle SSH globale existe déjà."
            echo "Supprime-la manuellement avant d'activer le mode Tailscale."
            return 1
        fi

        ufw allow in on tailscale0 \
            to any port "$SSH_PORT" proto tcp

    else

        ufw limit "${SSH_PORT}/tcp"

    fi

    # Politique par défaut uniquement si UFW n'est pas déjà actif.
    if [ "$active" = false ]; then
        ufw default deny incoming
        ufw default allow outgoing
    fi

    # Ports Web
    if [ "$OPEN_WEB" = "true" ]; then
        ufw allow 80/tcp
        ufw allow 443/tcp
        ufw allow 443/udp
    fi

    ufw --force enable
}

# ==========================================================
# FAIL2BAN
# ==========================================================

configure_fail2ban() {

    install -d -m 0755 /etc/fail2ban/jail.d

    cat > /etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
backend = systemd
port = $SSH_PORT
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban
}

# ==========================================================
# UNATTENDED UPGRADES
# ==========================================================

configure_unattended() {

    dpkg-reconfigure -f noninteractive unattended-upgrades

    systemctl enable --now apt-daily.timer
    systemctl enable --now apt-daily-upgrade.timer
}

# ==========================================================
# PATH UTILISATEUR
# ==========================================================

configure_user_path() {

    touch "$BASHRC"

    if ! grep -qF 'export PNPM_HOME=' "$BASHRC"; then
        {
            echo ""
            echo "# pnpm global binaries"
            echo "export PNPM_HOME=\"$PNPM_HOME\""
            echo 'export PATH="$PNPM_HOME/bin:$PATH"'
        } >> "$BASHRC"
    fi

    chown "$TARGET_USER:$USER_GROUP" "$BASHRC"
}

# ==========================================================
# VERIFICATION
# ==========================================================

ver() {
    "$@" 2>/dev/null | head -n 1 || true
}

check_services() {

    local service

    local services=(docker caddy fail2ban)
    if [ "$INSTALL_SYSTEM_DB" = "true" ]; then
        services+=(postgresql redis-server)
    fi

    for service in "${services[@]}"; do

        if ! systemctl is-active --quiet "$service"; then
            echo "Service inactif : $service"
            return 1
        fi

    done

    if ! ufw status | grep -q "Status: active"; then
        echo "Attention : UFW n'est pas actif."
        return 1
    fi
}

# ==========================================================
# BANNIERE
# ==========================================================

echo ""
echo -e "${BLUE}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║       HOMEBOX DEV ENVIRONMENT        ║"
echo "  ║          Ubuntu 26.04 LTS            ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${RESET}"
echo ""

# ==========================================================
# 1. SYSTEME
# ==========================================================

run_step "Mise à jour Ubuntu" \
    apt-get update

run_step "Installation des prérequis" \
    apt-get install -y \
    curl wget ca-certificates gnupg \
    lsb-release apt-transport-https \
    software-properties-common

# ==========================================================
# 2. GIT ET GIT FLOW
# ==========================================================

run_step "Installation Git et Git Flow" \
    apt-get install -y git git-flow

# ==========================================================
# 3. GITHUB CLI
# ==========================================================

run_step "Configuration du dépôt GitHub CLI" \
    setup_github_cli_repo

run_step "Installation GitHub CLI" \
    apt-get install -y gh

# ==========================================================
# 4. DOCKER ET COMPOSE
# ==========================================================

run_step "Configuration du dépôt Docker" \
    setup_docker_repo

run_step "Installation Docker et Compose" \
    apt-get install -y \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

run_step "Activation Docker" \
    systemctl enable --now docker

run_step "Configuration du démon Docker" \
    configure_docker_daemon

if [ "$ADD_USER_TO_DOCKER_GROUP" = "true" ]; then

    run_step "Ajout de l'utilisateur au groupe Docker" \
        usermod -aG docker "$TARGET_USER"

fi

# ==========================================================
# 5. NODE.JS ET NPM
# ==========================================================

run_step "Configuration Node.js 24 LTS" \
    setup_nodesource

run_step "Installation Node.js et npm" \
    apt-get install -y nodejs

# ==========================================================
# 6. PNPM
# ==========================================================

run_step "Installation pnpm" \
    npm install -g --allow-scripts=pnpm pnpm

run_step "Préparation du dossier pnpm" \
    prepare_pnpm_dir

run_step "Configuration pnpm global" \
    pnpm_as_user config set global-bin-dir "$PNPM_BIN"

# ==========================================================
# 7. VERCEL CLI
# ==========================================================

run_step "Installation Vercel CLI" \
    pnpm_as_user add -g vercel

# ==========================================================
# 8-9. POSTGRESQL ET REDIS (système, optionnels)
# ==========================================================

if [ "$INSTALL_SYSTEM_DB" = "true" ]; then

    run_step "Installation PostgreSQL" \
        apt-get install -y postgresql postgresql-contrib

    run_step "Activation PostgreSQL" \
        systemctl enable --now postgresql

    run_step "Installation Redis" \
        apt-get install -y redis-server

    run_step "Activation Redis" \
        systemctl enable --now redis-server

else

    # Seuls les clients, pour se connecter aux conteneurs
    run_step "Installation des clients PostgreSQL et Redis" \
        apt-get install -y postgresql-client redis-tools

fi

# ==========================================================
# 10. CADDY
# ==========================================================

run_step "Configuration du dépôt Caddy" \
    setup_caddy_repo

run_step "Installation Caddy" \
    apt-get install -y caddy

run_step "Activation Caddy" \
    systemctl enable --now caddy

# ==========================================================
# 11. SECURITE UBUNTU
# ==========================================================

run_step "Installation des outils de sécurité" \
    apt-get install -y \
    ufw fail2ban unattended-upgrades

run_step "Configuration du pare-feu UFW" \
    configure_firewall

run_step "Configuration Fail2ban SSH" \
    configure_fail2ban

run_step "Configuration des mises à jour automatiques" \
    configure_unattended

# Clés SSH uniquement. harden-ssh.sh refuse de s'appliquer (sans rien
# modifier) tant qu'aucune clé n'est autorisée : on continue alors.
SSH_HARDENED=false

if [ "$HARDEN_SSH" = "true" ] && [ -f "$SCRIPT_DIR/harden-ssh.sh" ]; then
    if bash "$SCRIPT_DIR/harden-ssh.sh" >> "$PERSIST_LOG" 2>&1; then
        SSH_HARDENED=true
        echo -e "${GREEN}✔${RESET} Durcissement SSH terminé."
    else
        echo -e "${YELLOW}!${RESET} Durcissement SSH reporté (aucune clé SSH autorisée, voir harden-ssh.sh)."
    fi
fi

# ==========================================================
# 12. PATH
# ==========================================================

run_step "Configuration PATH utilisateur" \
    configure_user_path

# ==========================================================
# 13. VERIFICATION DES SERVICES
# ==========================================================

run_step "Vérification des services" \
    check_services

# ==========================================================
# 14. VERSIONS
# ==========================================================

echo ""
echo -e "${GREEN}Installation terminée !${RESET}"
echo ""
echo "========== VERSIONS =========="
echo "Git       : $(ver git --version)"
echo "Git Flow  : $(ver git flow version)"
echo "GitHub    : $(ver gh --version)"
echo "Node.js   : $(ver node -v)"
echo "npm       : $(ver npm -v)"
echo "pnpm      : $(ver pnpm -v)"

echo "Vercel    : $(ver runuser -u "$TARGET_USER" -- env HOME="$USER_HOME" PATH="$PNPM_BIN:/usr/local/bin:/usr/bin:/bin" vercel --version)"

echo "Docker    : $(ver docker --version)"
echo "Compose   : $(ver docker compose version --short)"
echo "PostgreSQL: $(ver psql --version)"
echo "Redis     : $(redis-cli --version 2>/dev/null | awk '{print $2}' || true)"
echo "Caddy     : $(ver caddy version)"
echo "=============================="
echo ""

echo "UFW       : $(ufw status | head -n 1)"
echo "Fail2ban  : $(ver fail2ban-client --version)"
echo ""

echo -e "${GREEN}✔ HOMEBOX DEV ENVIRONMENT PRÊT !${RESET}"
echo ""
echo "Utilisateur : $TARGET_USER"
echo "PATH        : $PNPM_BIN"
echo "Log         : $PERSIST_LOG"
echo ""

if [ "$ADD_USER_TO_DOCKER_GROUP" = "true" ]; then
    echo -e "${YELLOW}Attention : le groupe Docker donne des privilèges équivalents à root.${RESET}"
fi

echo -e "${YELLOW}Les ports Docker sont publiés sur 127.0.0.1 par défaut (/etc/docker/daemon.json).${RESET}"

if [ "$SSH_HARDENED" = "true" ]; then
    echo -e "${YELLOW}SSH : clés uniquement. Garde cette session ouverte et teste une nouvelle connexion.${RESET}"
else
    echo -e "${YELLOW}SSH accepte encore les mots de passe : ajoute ta clé (ssh-copy-id) puis sudo bash ./harden-ssh.sh${RESET}"
fi
echo ""
echo "Reconnecte-toi pour appliquer les groupes et le PATH."
echo ""

