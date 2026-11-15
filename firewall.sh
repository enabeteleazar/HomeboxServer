#!/bin/bash
# ==========================================================
# HOMEBOX : pare-feu UFW
# Usage : sudo bash ./firewall.sh            (applique et active)
#         sudo bash ./firewall.sh --dry-run  (affiche seulement les règles)
#
# Politique :
#   - tout ce qui entre est refusé par défaut, tout ce qui sort est autorisé ;
#   - SSH ouvert (limité contre la force brute), ou seulement via Tailscale ;
#   - 80/443 ouverts pour Caddy (sites publiés) ;
#   - réseau local et Tailscale considérés de confiance : dashboard (5199),
#     serveurs de dev des projets, etc. y restent joignables ;
#   - le reste (Internet) est bloqué.
#
# Options (variables d'environnement) :
#   OPEN_WEB=false           ne pas ouvrir 80/443
#   TRUST_LAN=false          ne pas autoriser le réseau local
#   TRUST_TAILSCALE=false    ne pas autoriser l'interface tailscale0
#   SSH_TAILSCALE_ONLY=true  SSH seulement via Tailscale (et le réseau local si TRUST_LAN)
#
# Rappel : Docker contourne UFW. Les conteneurs HOMEBOX publient leurs ports
# sur 127.0.0.1 (et /etc/docker/daemon.json en fait le défaut).
# ==========================================================

set -Eeuo pipefail

OPEN_WEB="${OPEN_WEB:-true}"
TRUST_LAN="${TRUST_LAN:-true}"
TRUST_TAILSCALE="${TRUST_TAILSCALE:-true}"
SSH_TAILSCALE_ONLY="${SSH_TAILSCALE_ONLY:-false}"

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

for option in OPEN_WEB TRUST_LAN TRUST_TAILSCALE SSH_TAILSCALE_ONLY; do
    if [[ "${!option}" != "true" && "${!option}" != "false" ]]; then
        echo "Erreur : $option doit être true ou false."
        exit 1
    fi
done

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : utilise sudo."
    exit 1
fi

command -v ufw >/dev/null 2>&1 || { echo "UFW n'est pas installé (sudo apt install ufw)."; exit 1; }

# ----------------------------------------------------------
# Détection
# ----------------------------------------------------------

SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" && !found {print $2; found = 1}' || true)"
SSH_PORT="${SSH_PORT:-22}"

LAN_IF="$(ip -4 route show default | awk '!found {for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); found = 1; break}}' || true)"
LAN_NET=""
if [ -n "$LAN_IF" ]; then
    LAN_NET="$(ip -4 route show dev "$LAN_IF" proto kernel scope link | awk 'NR == 1 {print $1}' || true)"
fi

HAS_TAILSCALE=false
ip link show tailscale0 >/dev/null 2>&1 && HAS_TAILSCALE=true

CLIENT_IP=""
[ -n "${SSH_CONNECTION:-}" ] && CLIENT_IP="${SSH_CONNECTION%% *}"

if [ "$TRUST_LAN" = "true" ] && [ -z "$LAN_NET" ]; then
    echo "Réseau local introuvable (pas de route par défaut) : relance avec TRUST_LAN=false."
    exit 1
fi

if [ "$SSH_TAILSCALE_ONLY" = "true" ] && [ "$HAS_TAILSCALE" = "false" ]; then
    echo "SSH_TAILSCALE_ONLY=true mais l'interface tailscale0 n'existe pas : refus (tu perdrais l'accès)."
    exit 1
fi

# ----------------------------------------------------------
# Règles
# ----------------------------------------------------------

RULES=()

RULES+=("default deny incoming")
RULES+=("default allow outgoing")

if [ "$SSH_TAILSCALE_ONLY" = "true" ]; then
    RULES+=("allow in on tailscale0 to any port $SSH_PORT proto tcp comment homebox-ssh-tailscale")
    [ "$TRUST_LAN" = "true" ] && RULES+=("allow from $LAN_NET to any port $SSH_PORT proto tcp comment homebox-ssh-lan")
else
    RULES+=("limit $SSH_PORT/tcp comment homebox-ssh")
fi

if [ "$OPEN_WEB" = "true" ]; then
    RULES+=("allow 80/tcp comment homebox-http")
    RULES+=("allow 443/tcp comment homebox-https")
    RULES+=("allow 443/udp comment homebox-http3")
fi

if [ "$TRUST_TAILSCALE" = "true" ] && [ "$HAS_TAILSCALE" = "true" ]; then
    RULES+=("allow in on tailscale0 comment homebox-tailscale")
fi

if [ "$TRUST_LAN" = "true" ]; then
    RULES+=("allow from $LAN_NET comment homebox-lan")
fi

# ----------------------------------------------------------
# Vérification : la session SSH actuelle reste-t-elle autorisée ?
# ----------------------------------------------------------

if [ -n "$CLIENT_IP" ] && [ "$SSH_TAILSCALE_ONLY" = "true" ]; then
    route="$(ip route get "$CLIENT_IP" 2>/dev/null || true)"
    via_tailscale=false
    echo "$route" | grep -q 'dev tailscale0' && via_tailscale=true
    in_lan=false
    if [ "$TRUST_LAN" = "true" ] && python3 -c "import ipaddress,sys; sys.exit(0 if ipaddress.ip_address('$CLIENT_IP') in ipaddress.ip_network('$LAN_NET') else 1)" 2>/dev/null; then
        in_lan=true
    fi
    if ! $via_tailscale && ! $in_lan; then
        echo "Ta session SSH ($CLIENT_IP) ne passe ni par Tailscale ni par le réseau local :"
        echo "avec SSH_TAILSCALE_ONLY=true tu perdrais l'accès. Rien n'a été modifié."
        exit 1
    fi
fi

echo "Port SSH          : $SSH_PORT"
echo "Réseau local      : ${LAN_NET:-aucun} (${LAN_IF:-?}) — confiance : $TRUST_LAN"
echo "Tailscale         : $HAS_TAILSCALE — confiance : $TRUST_TAILSCALE"
echo "Session SSH       : ${CLIENT_IP:-aucune détectée}"
echo ""
echo "Règles :"
printf '  ufw %s\n' "${RULES[@]}"
echo ""

if $DRY_RUN; then
    echo "(--dry-run : rien n'a été appliqué)"
    exit 0
fi

for rule in "${RULES[@]}"; do
    # shellcheck disable=SC2086
    ufw $rule >/dev/null
done

ufw --force enable >/dev/null
systemctl enable --now ufw >/dev/null 2>&1 || true

echo "Pare-feu actif."
echo ""
ufw status verbose
echo ""
echo "Garde cette session ouverte et vérifie dans un autre terminal que SSH répond toujours."
echo "En cas de problème : sudo ufw disable"
