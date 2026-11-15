"""Page Caddy : sites publiés, certificats, journal, réglages.

Les modifications passent par `sudo -n /usr/local/sbin/homebox-caddy …` (partie
infra/), seul programme autorisé avec homebox-firewall : il valide les
paramètres, vérifie la configuration avec « caddy validate » et remet l'ancienne
en place si Caddy la refuse.
Le journal est lu directement (l'utilisateur est dans le groupe adm) et les
certificats sont vérifiés en se connectant localement à Caddy.
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from srvctl import journal

HELPER = "/usr/local/sbin/homebox-caddy"
SETUP_HINT = "cd /srv/HomeboxServer && make tools"
CERT_TTL = 600

_cert_cache: dict[str, tuple[float, dict]] = {}


class CaddyError(Exception):
    def __init__(self, message: str, missing: bool = False):
        super().__init__(message)
        self.message = message
        self.missing = missing


def _call(args: list[str], timeout: int = 90) -> dict:
    try:
        proc = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CaddyError("sudo est introuvable sur ce système.", missing=True)
    except subprocess.TimeoutExpired:
        raise CaddyError("Caddy n'a pas répondu à temps.")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not data:
        err = (proc.stderr or proc.stdout or "").strip()
        markers = ("password is required", "interactive authentication is required", "not allowed",
                   "command not found", "No such file")
        missing = any(m in err for m in markers)
        raise CaddyError("Le dashboard n'a pas encore accès à Caddy." if missing else f"Erreur : {err[:300]}",
                         missing=missing)
    return data


def _logged(action: str, args: list[str], source: str) -> dict:
    activity_id = journal.start_activity(None, action, source)
    started = time.monotonic()
    try:
        data = _call(args)
    except CaddyError as exc:
        journal.finish_activity(activity_id, False, 1, exc.message, int((time.monotonic() - started) * 1000))
        raise
    journal.finish_activity(activity_id, bool(data.get("ok")), 0 if data.get("ok") else 1,
                            f"$ homebox-caddy {' '.join(args)}\n{data.get('message', '')}",
                            int((time.monotonic() - started) * 1000))
    return data


def add_site(domain: str, port: str = "", static: str = "", source: str = "dashboard") -> dict:
    args = ["add", "--domain", domain]
    args += ["--port", port] if port else ["--static", static]
    return _logged("caddy-add", args, source)


def remove_site(domain: str, source: str = "dashboard") -> dict:
    return _logged("caddy-remove", ["remove", "--domain", domain], source)


def reload(source: str = "dashboard") -> dict:
    return _logged("caddy-reload", ["reload"], source)


def set_email(email: str, source: str = "dashboard") -> dict:
    return _logged("caddy-email", ["set-email", "--email", email], source)


# --- vérifications locales ---------------------------------------------------

def certificate(domain: str) -> dict:
    """Certificat que Caddy sert pour ce domaine (connexion locale, SNI)."""
    cached = _cert_cache.get(domain)
    if cached and time.time() - cached[0] < CERT_TTL:
        return cached[1]
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=4) as raw:
            with ctx.wrap_socket(raw, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        issuer = dict(x[0] for x in cert.get("issuer", ()))
        days = (expires - datetime.now(timezone.utc)).days
        result = {"ok": True, "expires": expires.isoformat(), "days_left": days,
                  "issuer": issuer.get("organizationName") or issuer.get("commonName", ""),
                  "level": "ok" if days >= 14 else ("warn" if days >= 7 else "crit")}
    except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
        text = str(exc)
        hint = ("pas encore de certificat : le domaine pointe-t-il vers cette machine, ports 80/443 ouverts ?"
                if "internal error" in text or "alert" in text.lower() or "verify" in text.lower() else text[:160])
        result = {"ok": False, "error": hint, "level": "crit"}
    _cert_cache[domain] = (time.time(), result)
    return result


def port_open(port: Optional[int]) -> Optional[bool]:
    if not port:
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def logs(lines: int = 60) -> list[dict]:
    """Dernières lignes du journal de Caddy (JSON ou texte systemd)."""
    try:
        out = subprocess.run(["journalctl", "-u", "caddy", "-n", str(lines), "--no-pager", "-o", "json"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    entries = []
    for line in out.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("MESSAGE", "")
        if isinstance(message, list):  # journald encode parfois les messages binaires
            message = bytes(message).decode("utf-8", "replace")
        ts = datetime.fromtimestamp(int(record.get("__REALTIME_TIMESTAMP", 0)) / 1e6, timezone.utc).isoformat()
        entry = {"time": ts, "level": "info", "logger": "systemd", "message": message}
        try:
            payload = json.loads(message)
            entry.update(level=payload.get("level", "info"), logger=payload.get("logger", ""),
                         message=payload.get("msg", message))
            details = [f"{k}={payload[k]}" for k in ("host", "uri", "status", "error", "domain", "identifiers")
                       if payload.get(k)]
            if details:
                entry["details"] = " · ".join(str(d) for d in details)[:300]
        except (json.JSONDecodeError, AttributeError):
            pass
        entries.append(entry)
    return list(reversed(entries))


# --- vue complète ------------------------------------------------------------

def overview() -> dict:
    result: dict = {"available": False, "missing": False, "error": None, "setup_hint": SETUP_HINT}
    try:
        data = _call(["status"])
        if not data.get("ok"):
            raise CaddyError(data.get("message", "lecture impossible"))
        status = data["status"]
        result.update(available=True, status=status)
    except CaddyError as exc:
        result.update(error=exc.message, missing=exc.missing)
        status = {"sites": []}

    projects = {}
    try:
        from srvctl.srvctl import load_registry
        for p in load_registry().values():
            if p.get("port"):
                projects[int(p["port"])] = p["name"]
    except Exception:
        pass

    sites = []
    for site in status.get("sites", []):
        port = site.get("port")
        sites.append({
            **site,
            "project": projects.get(port) if port else None,
            "upstream_up": port_open(port) if site.get("mode") == "proxy" else None,
            "certificate": certificate(site["domain"]),
        })
    result["sites"] = sites
    result["projects"] = sorted(({"name": n, "port": p} for p, n in projects.items()), key=lambda x: x["name"].lower())
    result["logs"] = logs()
    result["log_errors"] = sum(1 for e in result["logs"] if e["level"] in ("error", "fatal", "warn"))
    return result
