#!/bin/bash
# ==========================================================
# HOMEBOX : désactive PostgreSQL et Redis installés via apt
# (doublons des conteneurs Docker, ports 5432 et 6379).
# Usage : sudo bash ./remove-system-db.sh
#
# Les données sont d'abord sauvegardées. Les services sont
# seulement arrêtés et désactivés, rien n'est désinstallé :
#   réactiver : sudo systemctl enable --now postgresql redis-server
# ==========================================================

set -Eeuo pipefail
umask 077

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : utilise sudo."
    exit 1
fi

OUT="${BACKUP_ROOT:-/srv/backups}/system-db-$(date '+%Y%m%d-%H%M%S')"
install -d -m 0700 "$OUT"

# ---------- PostgreSQL ----------
if systemctl is-active --quiet postgresql; then
    dbs="$(runuser -u postgres -- psql -AtX -c \
        "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'" || true)"

    if [ -n "$dbs" ]; then
        echo "Bases PostgreSQL système trouvées : $(echo "$dbs" | paste -sd ' ')"
    else
        echo "PostgreSQL système : aucune base utilisateur."
    fi

    runuser -u postgres -- pg_dumpall > "$OUT/postgresql-system.sql"
    echo "Sauvegarde : $OUT/postgresql-system.sql"

    systemctl disable --now postgresql
    echo "PostgreSQL système arrêté et désactivé."
else
    echo "PostgreSQL système : déjà arrêté."
fi

# ---------- Redis ----------
if systemctl is-active --quiet redis-server; then
    keys="$(redis-cli dbsize 2>/dev/null || echo "?")"
    echo "Redis système : $keys clé(s)."

    if [ "$keys" != "0" ] && redis-cli --rdb "$OUT/redis-system.rdb" >/dev/null 2>&1; then
        echo "Sauvegarde : $OUT/redis-system.rdb"
    fi

    systemctl disable --now redis-server
    echo "Redis système arrêté et désactivé."
else
    echo "Redis système : déjà arrêté."
fi

rmdir "$OUT" 2>/dev/null || true

echo ""
echo "Terminé. Utilise désormais les conteneurs :"
echo "  PostgreSQL 127.0.0.1:5433   (homebox-project create <nom> pour une base)"
echo "  Redis      127.0.0.1:6380"
