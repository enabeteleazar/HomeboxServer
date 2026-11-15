"""Authentification de l'interface web : un mot de passe + une session signée.

- Le mot de passe n'est jamais stocké : seulement son empreinte scrypt,
  dans <SRV_ROOT>/.env sous DASHBOARD_PASSWORD_HASH.
- La session est un cookie signé (HMAC-SHA256) avec DASHBOARD_SESSION_SECRET,
  valable SESSION_DAYS jours. La signature inclut l'empreinte du mot de passe :
  changer le mot de passe déconnecte toutes les sessions ouvertes.
- Sans mot de passe configuré, l'interface est fermée (jamais « ouverte par
  défaut ») : `make dashboard-password` sur homebox pour le définir.
- L'API /api/v1 garde son propre jeton Bearer (auth.py) et n'est pas concernée.

Uniquement la bibliothèque standard : aucune dépendance à ajouter.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from srvctl.srvctl import get_root

PASSWORD_KEY = "DASHBOARD_PASSWORD_HASH"
SECRET_KEY = "DASHBOARD_SESSION_SECRET"

COOKIE_NAME = "srv_session"
SESSION_DAYS = 7
SESSION_SECONDS = SESSION_DAYS * 24 * 3600

MIN_PASSWORD_LENGTH = 10

# Anti force brute : au-delà de MAX_FAILURES échecs en FAILURE_WINDOW
# secondes, l'adresse est bloquée jusqu'à ce que les échecs vieillissent.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

# Paramètres scrypt (≈ 16 Mo de mémoire, ~50 ms par vérification)
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


# --- lecture / écriture de <SRV_ROOT>/.env ---------------------------------

def env_path() -> Path:
    return get_root() / ".env"


def read_env_value(key: str) -> Optional[str]:
    """Variable d'environnement d'abord, puis <SRV_ROOT>/.env (relu à chaque
    appel : un changement de mot de passe ne demande pas de redémarrage)."""
    value = os.environ.get(key)
    if value and value.strip():
        return value.strip()
    path = env_path()
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    except OSError:
        return None
    return None


def write_env_value(key: str, value: str) -> None:
    """Remplace (ou ajoute) KEY=value dans <SRV_ROOT>/.env, fichier en 0600."""
    path = env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --- mot de passe ----------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        computed = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(_unb64(digest)),
        )
        return hmac.compare_digest(computed, _unb64(digest))
    except (ValueError, TypeError):
        return False


def password_hash() -> Optional[str]:
    return read_env_value(PASSWORD_KEY)


def session_secret() -> Optional[str]:
    return read_env_value(SECRET_KEY)


def is_configured() -> bool:
    return bool(password_hash() and session_secret())


def set_password(password: str) -> None:
    """Enregistre un nouveau mot de passe (et crée le secret de session au besoin)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"au moins {MIN_PASSWORD_LENGTH} caractères")
    if not session_secret():
        write_env_value(SECRET_KEY, secrets.token_urlsafe(48))
    write_env_value(PASSWORD_KEY, hash_password(password))


# --- session ---------------------------------------------------------------

def _sign(payload: str) -> Optional[str]:
    secret, pw_hash = session_secret(), password_hash()
    if not secret or not pw_hash:
        return None
    key = (secret + "|" + pw_hash).encode()
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def new_session_token() -> str:
    expires = str(int(time.time()) + SESSION_SECONDS)
    signature = _sign(expires)
    if signature is None:
        raise RuntimeError("authentification non configurée")
    return f"{expires}.{signature}"


def session_valid(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = _sign(expires)
    return expected is not None and hmac.compare_digest(signature, expected)


# --- limitation des tentatives ---------------------------------------------

_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def _recent(ip: str, now: float) -> list[float]:
    attempts = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    if attempts:
        _failures[ip] = attempts
    else:
        _failures.pop(ip, None)
    return attempts


def retry_after(ip: str) -> int:
    """Secondes avant de pouvoir réessayer (0 si l'adresse n'est pas bloquée)."""
    now = time.time()
    with _lock:
        attempts = _recent(ip, now)
        if len(attempts) < MAX_FAILURES:
            return 0
        return max(1, int(FAILURE_WINDOW - (now - attempts[-MAX_FAILURES])))


def record_failure(ip: str) -> None:
    with _lock:
        _failures.setdefault(ip, []).append(time.time())


def reset_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)


def check_login(ip: str, password: str) -> bool:
    stored = password_hash()
    if not stored:
        return False
    if verify_password(password, stored):
        reset_failures(ip)
        return True
    record_failure(ip)
    return False
