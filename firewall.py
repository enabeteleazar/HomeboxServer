"""Page Pare-feu : lecture et gestion d'UFW via le script root homebox-firewall.

Le dashboard tourne sous un utilisateur normal : toute opération passe par
`sudo -n /usr/local/sbin/homebox-firewall …`, seul programme autorisé par la
règle /etc/sudoers.d/homebox-firewall (installée par infra-docker.sh). C'est
ce script qui valide les paramètres et protège les règles « homebox-… ».

En plus des règles, on calcule l'exposition réelle de chaque port en écoute :
local seulement, réseau local / Tailscale, ou Internet (y compris les ports
publiés par Docker, qui contourne UFW).
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import time
from typing import Optional

from srvctl import journal

HELPER = "/usr/local/sbin/homebox-firewall"
SETUP_HINT = "cd /srv/Projects/HomeboxServer && make tools"

# Services système connus (le nom du processus n'est visible qu'en root)
KNOWN_PORTS = {
    22: "SSH", 53: "DNS local (systemd-resolved)", 80: "Caddy (HTTP)", 443: "Caddy (HTTPS)",
    323: "chrony (heure)", 2019: "Caddy (administration)", 5199: "Dashboard",
    41641: "Tailscale", 8443: "Tailscale serve",
}
# Clients DHCP : pas des services, juste du bruit dans la liste
IGNORED_PORTS = {("udp", 68), ("udp", 546)}

EXPOSURE_LABELS = {
    "internet": "Internet",
    "trusted": "Réseau local / Tailscale",
    "local": "Machine seulement",
    "blocked": "Bloqué par le pare-feu",
}


class FirewallError(Exception):
    def __init__(self, message: str, missing: bool = False):
        super().__init__(message)
        self.message = message
        self.missing = missing


# --- appel du script root --------------------------------------------------

def _call(args: list[str], timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            ["sudo", "-n", HELPER, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise FirewallError("sudo est introuvable sur ce système.", missing=True)
    except subprocess.TimeoutExpired:
        raise FirewallError("Le pare-feu n'a pas répondu à temps.")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}

    if not data:
        err = (proc.stderr or proc.stdout or "").strip()
        markers = ("password is required", "interactive authentication is required", "not allowed",
                   "no new privileges", "command not found", "No such file")
        missing = any(marker in err for marker in markers)
        raise FirewallError(
            "Le dashboard n'a pas accès au pare-feu." if missing else f"Erreur du pare-feu : {err[:300]}",
            missing=missing,
        )
    return data


def _run_and_log(action: str, args: list[str], source: str, timeout: int = 60) -> dict:
    activity_id = journal.start_activity(None, action, source)
    started = time.monotonic()
    try:
        data = _call(args, timeout=timeout)
    except FirewallError as exc:
        journal.finish_activity(activity_id, False, 1, exc.message, int((time.monotonic() - started) * 1000))
        raise
    output = data.get("message", "")
    if data.get("command"):
        output = f"$ {data['command']}\n{output}"
    journal.finish_activity(activity_id, bool(data.get("ok")), 0 if data.get("ok") else 1, output,
                            int((time.monotonic() - started) * 1000))
    data["activity_id"] = activity_id
    return data


def status() -> dict:
    data = _call(["status"])
    if not data.get("ok"):
        raise FirewallError(data.get("message", "lecture impossible"))
    return data["status"]


def add_rule(action: str, port: str, proto: str, source: str, iface: str, comment: str,
             origin: str = "dashboard") -> dict:
    args = ["add", "--action", action, "--proto", proto, "--from", source or "any"]
    if port:
        args += ["--port", port]
    if iface:
        args += ["--in", iface]
    if comment:
        args += ["--comment", comment]
    return _run_and_log("firewall-add", args, origin)


def delete_rule(number: int, expect: str, origin: str = "dashboard") -> dict:
    return _run_and_log("firewall-delete", ["delete", "--number", str(number), "--expect", expect], origin)


def set_enabled(enabled: bool, origin: str = "dashboard") -> dict:
    return _run_and_log("firewall-enable" if enabled else "firewall-disable",
                        ["enable" if enabled else "disable"], origin)


def apply_policy(dry_run: bool, origin: str = "dashboard") -> dict:
    args = ["policy"] + (["--dry-run"] if dry_run else [])
    if dry_run:
        return _call(args, timeout=120)
    return _run_and_log("firewall-policy", args, origin, timeout=120)


# --- réseau local ----------------------------------------------------------

def lan_network() -> Optional[str]:
    """Réseau local de l'interface par défaut (ex. 192.168.1.0/24)."""
    try:
        default = subprocess.run(["ip", "-4", "route", "show", "default"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
        iface = default[default.index("dev") + 1]
        routes = subprocess.run(["ip", "-4", "route", "show", "dev", iface, "proto", "kernel", "scope", "link"],
                                capture_output=True, text=True, timeout=5).stdout.split()
        return routes[0] if routes else None
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return None


# --- ports en écoute et exposition -----------------------------------------

_SS_LINE = re.compile(r"^(?P<proto>tcp|udp)\s+\S+\s+\d+\s+\d+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<proc>.*))?$")


def _split_address(local: str) -> tuple[str, Optional[str], int]:
    """« 0.0.0.0:22 », « [::]:80 », « 100.73.78.48%tailscale0:8443 » → (adresse, interface, port)."""
    host, _, port = local.rpartition(":")
    iface = None
    if "%" in host:
        host, iface = host.split("%", 1)
    return host.strip("[]"), iface, int(port) if port.isdigit() else 0


def listening_sockets() -> list[dict]:
    try:
        out = subprocess.run(["ss", "-H", "-ltnup"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    seen: dict[tuple, dict] = {}
    for line in out.splitlines():
        m = _SS_LINE.match(line.strip())
        if not m:
            continue
        host, iface, port = _split_address(m.group("local"))
        if not port or (m.group("proto"), port) in IGNORED_PORTS:
            continue
        proc = m.group("proc") or ""
        name = re.search(r'\(\("([^"]+)"', proc)
        key = (m.group("proto"), host, iface, port)
        seen[key] = {
            "proto": m.group("proto"), "address": host, "iface": iface, "port": port,
            "process": name.group(1) if name else "",
        }
    return sorted(seen.values(), key=lambda s: (s["port"], s["proto"], s["address"]))


def docker_ports() -> tuple[dict[int, str], dict[int, str]]:
    """Ports publiés par Docker → nom du conteneur : (sur toutes les interfaces, tous)."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    public, every = {}, {}
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        for m in re.finditer(r"(?:^|, )(?P<host>[0-9a-fA-F.:\[\]]*):(?P<low>\d+)(?:-(?P<high>\d+))?->", ports):
            low, high = int(m.group("low")), int(m.group("high") or m.group("low"))
            exposed = m.group("host") in ("0.0.0.0", "::", "[::]", "")
            for p in range(low, high + 1):
                every[p] = name
                if exposed:
                    public[p] = name
    return public, every


def _service_names() -> dict[int, str]:
    """Port → nom lisible : projets du registre, puis services connus."""
    names = dict(KNOWN_PORTS)
    try:
        from srvctl.srvctl import load_registry
        for project in load_registry().values():
            if project.get("port"):
                names[int(project["port"])] = f"Projet {project['name']}"
    except Exception:  # registre illisible : on se contente des noms connus
        pass
    return names


def _parse_target(to: str) -> tuple[Optional[tuple[int, int]], Optional[str], Optional[str]]:
    """Destination UFW → (plage de ports ou None = tous, protocole, interface)."""
    iface = None
    if " on " in to:
        to, iface = to.split(" on ", 1)
        iface = iface.strip()
    to = to.strip()
    m = re.search(r"(\d+)(?::(\d+))?(?:/(tcp|udp))?$", to)
    if not m or to == "Anywhere":
        return None, None, iface
    return (int(m.group(1)), int(m.group(2) or m.group(1))), m.group(3), iface


def _rule_covers(rule: dict, port: int, proto: str) -> bool:
    ports, rproto, _ = _parse_target(rule["to"])
    if rproto and rproto != proto:
        return False
    return ports is None or ports[0] <= port <= ports[1]


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address in ("localhost",)


def exposure(socket: dict, fw: dict, docker_ports: dict[int, str]) -> tuple[str, str]:
    """Niveau d'exposition d'un port en écoute + explication courte."""
    if _is_loopback(socket["address"]) or socket["iface"] == "lo":
        return "local", "écoute sur 127.0.0.1 seulement"

    if socket["port"] in docker_ports:
        return "internet", f"publié par Docker ({docker_ports[socket['port']]}) : Docker contourne UFW"

    if socket["iface"] == "tailscale0" or socket["address"].startswith("100."):
        return "trusted", "écoute sur l'adresse Tailscale seulement"

    if not fw.get("active"):
        return "internet", "pare-feu inactif"

    # UFW applique la première règle qui correspond. Pour une connexion venant
    # d'Internet, seules comptent les règles « depuis n'importe où » ; les règles
    # de confiance (réseau local, Tailscale) n'ouvrent que pour leurs sources.
    for rule in fw.get("rules", []):
        if rule["direction"] != "IN" or not _rule_covers(rule, socket["port"], socket["proto"]):
            continue
        _, _, iface = _parse_target(rule["to"])
        from_anywhere = rule["from"] == "Anywhere" and not iface
        if from_anywhere:
            if rule["action"] in ("ALLOW", "LIMIT"):
                label = "ouvert à Internet" + (" (limité)" if rule["action"] == "LIMIT" else "")
                return "internet", f"{label} — règle {rule['number']}"
            break

    trusted = any(
        r["action"] in ("ALLOW", "LIMIT") and r["direction"] == "IN" and _rule_covers(r, socket["port"], socket["proto"])
        and (r["from"] != "Anywhere" or _parse_target(r["to"])[2])
        for r in fw.get("rules", [])
    )
    if trusted:
        return "trusted", "autorisé depuis le réseau local ou Tailscale"
    return "blocked", "aucune règle ne l'autorise depuis l'extérieur"


# --- vue complète ----------------------------------------------------------

def overview() -> dict:
    """Tout ce qu'affiche la page. Ne lève jamais : les erreurs sont dans « error »."""
    result: dict = {"available": False, "missing": False, "error": None, "lan": lan_network(),
                    "setup_hint": SETUP_HINT}
    try:
        fw = status()
        result.update(available=True, fw=fw)
    except FirewallError as exc:
        result.update(error=exc.message, missing=exc.missing)
        fw = {"active": False, "rules": []}

    public_docker, all_docker = docker_ports()
    names = _service_names()
    sockets = []
    for s in listening_sockets():
        level, why = exposure(s, fw, public_docker) if result["available"] else ("unknown", "état du pare-feu inconnu")
        service = (f"Docker : {all_docker[s['port']]}" if s["port"] in all_docker
                   else names.get(s["port"]) or s["process"] or "—")
        sockets.append({**s, "service": service, "exposure": level, "why": why,
                        "label": EXPOSURE_LABELS.get(level, "inconnu")})
    result["sockets"] = sockets
    result["exposed_count"] = sum(1 for s in sockets if s["exposure"] == "internet")
    if result["available"]:
        rules = fw.get("rules", [])
        result["rule_count"] = len([r for r in rules if not r["v6"]])
        result["protected_count"] = len([r for r in rules if r["protected"] and not r["v6"]])
    return result
