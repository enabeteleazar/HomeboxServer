#!/bin/bash
# ==========================================================
# HOMEBOX : durcissement SSH
# Usage : sudo bash ./harden-ssh.sh
#
# Désactive les mots de passe et la connexion root : seules
# les clés SSH sont acceptées. Refuse de s'appliquer tant que
# l'utilisateur n'a pas de clé enregistrée (sinon blocage).
# Annuler : sudo rm /etc/ssh/sshd_config.d/00-homebox.conf && sudo systemctl reload ssh
# ==========================================================

set -Eeuo pipefail

CONF="/etc/ssh/sshd_config.d/00-homebox.conf"

if [ "$EUID" -ne 0 ]; then
    echo "Erreur : utilise sudo."
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"

if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "Erreur : lance avec sudo depuis ton utilisateur normal."
    exit 1
fi

USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
KEYS="$USER_HOME/.ssh/authorized_keys"

key_count="$(grep -cE '^[[:space:]]*(ssh-(ed25519|rsa|dss)|ecdsa-sha2-|sk-(ssh-ed25519|ecdsa-sha2-))' "$KEYS" 2>/dev/null || true)"

if [ "${key_count:-0}" -eq 0 ]; then
    cat <<MSG
Aucune clé SSH autorisée pour $TARGET_USER ($KEYS est vide ou absent).
Couper les mots de passe maintenant te bloquerait dehors : rien n'a été modifié.

Depuis TON POSTE (pas le serveur) :
  1. ssh-keygen -t ed25519            (si tu n'as pas encore de clé)
  2. ssh-copy-id $TARGET_USER@homebox
  3. ssh $TARGET_USER@homebox          (doit se connecter SANS mot de passe)
Puis relance : sudo bash ./harden-ssh.sh
MSG
    exit 1
fi

# Permissions attendues par sshd (sinon la clé est ignorée)
chown "$TARGET_USER:" "$USER_HOME/.ssh" "$KEYS"
chmod 700 "$USER_HOME/.ssh"
chmod 600 "$KEYS"

# Préfixe 00- : sshd garde la PREMIÈRE valeur lue, ce fichier passe
# donc avant 50-cloud-init.conf qui réactive parfois les mots de passe.
backup=""
[ -f "$CONF" ] && backup="$(cat "$CONF")"

cat > "$CONF" <<SSHD
# HOMEBOX : géré par harden-ssh.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
SSHD
chmod 644 "$CONF"

if ! sshd -t; then
    if [ -n "$backup" ]; then echo "$backup" > "$CONF"; else rm -f "$CONF"; fi
    echo "Configuration refusée par sshd : modification annulée."
    exit 1
fi

effective="$(sshd -T 2>/dev/null)"
if ! echo "$effective" | grep -qx 'passwordauthentication no'; then
    echo "Attention : un autre fichier de /etc/ssh/sshd_config.d/ force encore les mots de passe :"
    grep -il 'PasswordAuthentication' /etc/ssh/sshd_config.d/*.conf /etc/ssh/sshd_config || true
fi

systemctl try-reload-or-restart ssh.service

echo "SSH durci : clés uniquement ($key_count clé(s) autorisée(s) pour $TARGET_USER), root interdit."
echo ""
echo "GARDE CETTE SESSION OUVERTE et vérifie dans un autre terminal :"
echo "  ssh $TARGET_USER@homebox"
echo "En cas de problème, depuis cette session :"
echo "  sudo rm $CONF && sudo systemctl reload ssh"
