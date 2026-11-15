"""Suivi du serveur en temps réel : système, Docker, services systemd.

Tout est lu sans droits root (psutil, docker CLI — l'utilisateur est dans le
groupe docker —, systemctl). Les mesures lentes sont mises en cache et
rafraîchies en arrière-plan :
- docker stats (~2,5 s) : toutes les DOCKER_TTL secondes ;
- mises à jour apt (simulation, ~1,5 s) : toutes les APT_TTL secondes.

Les débits (disque, réseau) sont calculés entre deux appels successifs.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import psutil

DOCKER_TTL = 10
APT_TTL = 30 * 60

# Seuils d'alerte (pourcentage ou °C) : au-delà de warn → orange, de crit → rouge
THRESHOLDS = {
    "cpu": (75, 90),
    "memory": (80, 92),
    "swap": (30, 70),
    "disk": (80, 90),
    "temperature": (70, 85),
}

# Services systemd suivis (ceux qui n'existent pas sur la machine sont ignorés)
WATCHED_SERVICES = [
    ("srv-dashboard.service", "Dashboard"),
    ("docker.service", "Docker"),
    ("containerd.service", "containerd"),
    ("caddy.service", "Caddy (web)"),
    ("ssh.service", "SSH"),
    ("ssh.socket", "SSH (socket)"),
    ("tailscaled.service", "Tailscale"),
    ("ufw.service", "Pare-feu UFW"),
    ("fail2ban.service", "Fail2ban"),
    ("unattended-upgrades.service", "Mises à jour automatiques"),
    ("cron.service", "Cron"),
    ("systemd-timesyncd.service", "Synchronisation de l'heure"),
    ("chrony.service", "Synchronisation de l'heure (chrony)"),
    ("sysstat.service", "sysstat"),
]
TIMER_PREFIXES = ("homebox-", "apt-daily", "fstrim", "logrotate", "certbot", "sysstat")

IGNORED_NICS = ("lo", "veth", "docker", "br-")
IGNORED_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "efivarfs", "nsfs", "fuse.snapfuse"}

_lock = threading.Lock()


def level(kind: str, value: Optional[float]) -> str:
    """ok / warn / crit selon THRESHOLDS."""
    if value is None:
        return "unknown"
    warn, crit = THRESHOLDS[kind]
    return "crit" if value >= crit else "warn" if value >= warn else "ok"


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LANG": "C", "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --- débits : différence entre deux lectures --------------------------------

class _Rate:
    """Garde la lecture précédente d'un compteur pour en déduire un débit."""

    def __init__(self):
        self.previous: dict[str, tuple[float, tuple]] = {}

    def per_second(self, key: str, values: tuple) -> Optional[tuple]:
        now = time.monotonic()
        with _lock:
            prev = self.previous.get(key)
            self.previous[key] = (now, values)
        if not prev or now - prev[0] < 0.5:
            return None
        elapsed = now - prev[0]
        return tuple(max(0.0, (v - p) / elapsed) for v, p in zip(values, prev[1]))


_disk_rate = _Rate()
_net_rate = _Rate()

# Amorçage : le premier appel de cpu_percent renvoie toujours 0
psutil.cpu_percent(percpu=True)
for _p in psutil.process_iter():
    try:
        _p.cpu_percent(None)
    except psutil.Error:
        pass


# --- système -----------------------------------------------------------------

def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "?"


def _os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _temperatures() -> list[dict]:
    result = []
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return result
    for chip, entries in sensors.items():
        for i, entry in enumerate(entries):
            if entry.current is None or entry.current <= 0:
                continue
            label = entry.label or (chip if len(entries) == 1 else f"{chip} {i}")
            if chip == "coretemp" and label.startswith("Package"):
                label = "Processeur"
            elif chip == "coretemp" and label.startswith("Core"):
                label = "Cœur " + label.split()[-1]
            elif chip == "acpitz":
                label = "Carte mère (ACPI)"
            result.append({
                "label": label, "value": round(entry.current, 1),
                "high": entry.high, "critical": entry.critical,
                "level": level("temperature", entry.current),
            })
    return result


def _disks() -> list[dict]:
    disks, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in IGNORED_FS or part.mountpoint.startswith(("/snap", "/var/snap", "/run")):
            continue
        if part.device in seen and part.mountpoint != "/":
            continue
        seen.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({
            "mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
            "total": usage.total, "used": usage.used, "free": usage.free,
            "percent": round(usage.percent, 1), "level": level("disk", usage.percent),
        })
    return sorted(disks, key=lambda d: (d["mount"] != "/", d["mount"]))


def _disk_io() -> list[dict]:
    result = []
    try:
        counters = psutil.disk_io_counters(perdisk=True) or {}
    except OSError:
        return result
    for name, c in sorted(counters.items()):
        if re.match(r"^(loop|ram|zram|dm-)", name) or re.search(r"\d$", name) and not name.startswith("nvme"):
            continue  # partitions et périphériques virtuels : on garde le disque entier
        if name.startswith("nvme") and "p" in name[4:]:
            continue
        rate = _disk_rate.per_second(name, (c.read_bytes, c.write_bytes, c.busy_time if hasattr(c, "busy_time") else 0))
        busy = None
        if rate is not None and hasattr(c, "busy_time"):
            busy = min(100.0, rate[2] / 10)  # busy_time en ms par seconde → %
        rotational = None
        try:
            with open(f"/sys/block/{name}/queue/rotational", encoding="utf-8") as f:
                rotational = f.read().strip() == "1"
        except OSError:
            pass
        model = ""
        try:
            with open(f"/sys/block/{name}/device/model", encoding="utf-8") as f:
                model = f.read().strip()
        except OSError:
            pass
        result.append({
            "name": name, "model": model, "rotational": rotational,
            "read_per_s": rate[0] if rate else None, "write_per_s": rate[1] if rate else None,
            "busy_percent": round(busy, 1) if busy is not None else None,
        })
    return result


def _network() -> list[dict]:
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    result = []
    for name, c in sorted(psutil.net_io_counters(pernic=True).items()):
        if name.startswith(IGNORED_NICS):
            continue
        st = stats.get(name)
        rate = _net_rate.per_second(name, (c.bytes_recv, c.bytes_sent))
        ipv4 = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET]
        result.append({
            "name": name, "up": bool(st and st.isup), "speed": st.speed if st else 0,
            "ipv4": ipv4, "rx_per_s": rate[0] if rate else None, "tx_per_s": rate[1] if rate else None,
            "rx_total": c.bytes_recv, "tx_total": c.bytes_sent,
            "kind": "Tailscale" if name.startswith("tailscale") else
                    "Wi-Fi" if name.startswith(("wl", "wlan")) else
                    "Ethernet" if name.startswith(("en", "eth")) else "",
        })
    return result


def _top_processes(limit: int = 8) -> dict:
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cmdline"]):
        try:
            cpu = p.cpu_percent(None)
            mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
        except psutil.Error:
            continue
        cmd = " ".join(p.info["cmdline"] or [])[:120] or p.info["name"]
        procs.append({"pid": p.info["pid"], "name": p.info["name"], "user": p.info["username"] or "?",
                      "cpu": round(cpu, 1), "memory": mem, "command": cmd})
    by_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:limit]
    by_mem = sorted(procs, key=lambda x: x["memory"], reverse=True)[:limit]
    return {"by_cpu": by_cpu, "by_memory": by_mem, "count": len(procs)}


# --- mises à jour apt (lent : cache + arrière-plan) --------------------------

_apt = {"at": 0.0, "value": None, "running": False}


def _refresh_apt() -> None:
    out = _run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=60)
    packages = [line.split()[1] for line in out.splitlines() if line.startswith("Inst ")]
    security = [line.split()[1] for line in out.splitlines()
                if line.startswith("Inst ") and "-security" in line]
    with _lock:
        _apt.update(at=time.time(), running=False,
                    value={"count": len(packages), "security": len(security), "packages": packages[:50]})


def apt_updates() -> Optional[dict]:
    with _lock:
        stale = time.time() - _apt["at"] > APT_TTL
        if stale and not _apt["running"]:
            _apt["running"] = True
            threading.Thread(target=_refresh_apt, daemon=True).start()
        return _apt["value"]


def system() -> dict:
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    per_core = psutil.cpu_percent(percpu=True)
    total_cpu = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    load = os.getloadavg()
    cores = psutil.cpu_count() or 1
    freq = psutil.cpu_freq()
    boot = psutil.boot_time()
    temps = _temperatures()
    cpu_temp = next((t["value"] for t in temps if t["label"] == "Processeur"), None)
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "kernel": platform.release(),
        "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - boot),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
        "cpu": {
            "model": _cpu_model(), "cores": cores, "percent": total_cpu, "level": level("cpu", total_cpu),
            "per_core": [{"percent": round(v, 1), "level": level("cpu", v)} for v in per_core],
            "load": [round(v, 2) for v in load], "load_per_core": round(load[0] / cores, 2),
            "freq_mhz": round(freq.current) if freq else None, "freq_max_mhz": round(freq.max) if freq else None,
            "temperature": cpu_temp, "temperature_level": level("temperature", cpu_temp),
        },
        "memory": {
            "total": vm.total, "used": vm.total - vm.available, "available": vm.available,
            "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
            "percent": round(vm.percent, 1), "level": level("memory", vm.percent),
        },
        "swap": {"total": sw.total, "used": sw.used, "percent": round(sw.percent, 1),
                 "level": level("swap", sw.percent) if sw.total else "ok"},
        "temperatures": temps,
        "disks": _disks(),
        "disk_io": _disk_io(),
        "network": _network(),
        "processes": _top_processes(),
        "updates": apt_updates(),
    }


# --- Docker -------------------------------------------------------------------

_docker = {"at": 0.0, "stats": {}, "disk": [], "running": False}


def _refresh_docker_stats() -> None:
    stats = {}
    out = _run(["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"], timeout=30)
    for line in out.splitlines():
        try:
            s = json.loads(line)
        except json.JSONDecodeError:
            continue
        mem_used = s.get("MemUsage", "").split("/")[0].strip()
        stats[s.get("Name", "")] = {
            "cpu": _percent(s.get("CPUPerc")), "memory_percent": _percent(s.get("MemPerc")),
            "memory": mem_used, "net_io": s.get("NetIO", ""), "block_io": s.get("BlockIO", ""),
            "pids": s.get("PIDs", ""),
        }
    disk = []
    for line in _run(["docker", "system", "df", "--format", "{{json .}}"], timeout=30).splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        disk.append({"type": d.get("Type", ""), "count": d.get("TotalCount", ""), "active": d.get("Active", ""),
                     "size": d.get("Size", ""), "reclaimable": d.get("Reclaimable", "")})
    with _lock:
        _docker.update(at=time.time(), stats=stats, disk=disk, running=False)


def _percent(text: Optional[str]) -> Optional[float]:
    try:
        return round(float((text or "").rstrip("%")), 1)
    except ValueError:
        return None


def _docker_stats() -> dict:
    with _lock:
        stale = time.time() - _docker["at"] > DOCKER_TTL
        if stale and not _docker["running"]:
            _docker["running"] = True
            threading.Thread(target=_refresh_docker_stats, daemon=True).start()
        return _docker["stats"]


def _labels(text: str) -> dict:
    labels = {}
    for part in (text or "").split(","):
        key, _, value = part.partition("=")
        if key:
            labels[key] = value
    return labels


def docker() -> dict:
    version = _run(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
    if not version:
        return {"available": False, "error": "Docker ne répond pas (service arrêté ou accès refusé)."}

    stats = _docker_stats()
    containers = []
    for line in _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"]).splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        labels = _labels(c.get("Labels", ""))
        name = c.get("Names", "")
        state = c.get("State", "")
        health = c.get("HealthStatus") or ""
        if health in ("none", ""):
            health = "healthy" if "(healthy)" in c.get("Status", "") else \
                     "unhealthy" if "(unhealthy)" in c.get("Status", "") else ""
        status_level = ("crit" if state in ("exited", "dead") or health == "unhealthy"
                        else "warn" if state in ("restarting", "paused", "created") or health == "starting"
                        else "ok")
        ports = sorted({m.group(0) for m in re.finditer(r"[0-9a-fA-F.:\[\]]*:\d+->\d+/\w+", c.get("Ports", ""))})
        containers.append({
            "name": name, "image": c.get("Image", ""), "state": state, "status": c.get("Status", ""),
            "health": health, "level": status_level, "ports": ports,
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
            "created": c.get("CreatedAt", ""), "stats": stats.get(name),
        })
    containers.sort(key=lambda x: (x["project"] or "~", x["name"]))

    disk = _docker["disk"]

    volumes = []
    for line in _run(["docker", "volume", "ls", "--format", "{{json .}}"]).splitlines():
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        volumes.append({"name": v.get("Name", ""), "driver": v.get("Driver", "")})

    running = sum(1 for c in containers if c["state"] == "running")
    return {
        "available": True, "version": version, "containers": containers,
        "running": running, "total": len(containers),
        "problems": sum(1 for c in containers if c["level"] != "ok"),
        "disk": disk, "volumes": volumes,
        "stats_age": int(time.time() - _docker["at"]) if _docker["at"] else None,
    }


# --- services systemd -------------------------------------------------------

def _usec_to_iso(value: str) -> Optional[str]:
    try:
        usec = int(value)
    except (TypeError, ValueError):
        return None
    if usec <= 0:
        return None
    return datetime.fromtimestamp(usec / 1_000_000, timezone.utc).isoformat()


def _systemd_time(value: str) -> Optional[str]:
    """« Thu 2026-11-12 15:38:01 UTC » → ISO 8601 (systemctl lancé en LANG=C)."""
    value = (value or "").strip()
    if not value or value == "n/a":
        return None
    parts = value.split()
    try:
        dt = datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None
    if len(parts) > 3 and parts[3] != "UTC":
        # fuseau local : on s'appuie sur l'heure locale de la machine
        return dt.astimezone().isoformat()
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _show(units: list[str], props: list[str]) -> list[dict]:
    if not units:
        return []
    out = _run(["systemctl", "show", "--no-pager", "-p", ",".join(props), *units])
    blocks, current = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    return blocks


def services() -> dict:
    names = [u for u, _ in WATCHED_SERVICES]
    props = ["Id", "Description", "LoadState", "ActiveState", "SubState", "UnitFileState",
             "ActiveEnterTimestamp", "MainPID", "MemoryCurrent", "NRestarts", "Result"]
    labels = dict(WATCHED_SERVICES)
    items = []
    for info in _show(names, props):
        if info.get("LoadState") != "loaded":
            continue
        active = info.get("ActiveState", "")
        lvl = "ok" if active == "active" else "crit" if active == "failed" else \
              "warn" if active in ("activating", "deactivating", "reloading") else "off"
        memory = info.get("MemoryCurrent", "")
        items.append({
            "unit": info.get("Id", ""), "label": labels.get(info.get("Id", ""), info.get("Description", "")),
            "description": info.get("Description", ""), "active": active, "sub": info.get("SubState", ""),
            "enabled": info.get("UnitFileState", ""), "since": _systemd_time(info.get("ActiveEnterTimestamp", "")),
            "pid": int(info.get("MainPID") or 0) or None,
            "memory": int(memory) if memory.isdigit() else None,
            "restarts": int(info.get("NRestarts") or 0), "level": lvl,
        })

    # Tâches planifiées (timers) : dernière et prochaine exécution, résultat
    timers = []
    try:
        raw = json.loads(_run(["systemctl", "list-timers", "--all", "-o", "json"]) or "[]")
    except json.JSONDecodeError:
        raw = []
    raw = [t for t in raw if t.get("unit", "").startswith(TIMER_PREFIXES)]
    results = {s["Id"]: s for s in _show([t["activates"] for t in raw if t.get("activates")],
                                          ["Id", "Result", "ExecMainStatus", "ActiveState", "Description"])}
    for t in raw:
        svc = results.get(t.get("activates", ""), {})
        result = svc.get("Result", "")
        failed = svc.get("ActiveState") == "failed" or (result not in ("", "success"))
        timers.append({
            "unit": t.get("unit", ""), "service": t.get("activates", ""),
            "description": svc.get("Description", ""),
            "last": _usec_to_iso(t.get("last")), "next": _usec_to_iso(t.get("next")),
            "result": result or "—", "level": "crit" if failed else "ok",
        })
    timers.sort(key=lambda t: (not t["unit"].startswith("homebox-"), t["unit"]))

    failed_units = []
    for line in _run(["systemctl", "list-units", "--state=failed", "--plain", "--no-legend", "--no-pager"]).splitlines():
        parts = line.split(None, 4)
        if parts:
            failed_units.append({"unit": parts[0], "description": parts[4] if len(parts) > 4 else ""})

    return {
        "services": items, "timers": timers, "failed": failed_units,
        "problems": sum(1 for s in items if s["level"] in ("crit", "warn"))
                    + sum(1 for t in timers if t["level"] == "crit") + len(failed_units),
    }


# --- résumé pour la barre latérale / l'API ---------------------------------

def summary() -> dict:
    s = system()
    return {
        "cpu": s["cpu"]["percent"], "cpu_level": s["cpu"]["level"],
        "memory": s["memory"]["percent"], "memory_level": s["memory"]["level"],
        "disk": max((d["percent"] for d in s["disks"]), default=None),
        "disk_level": max((d["level"] for d in s["disks"]), key=["ok", "warn", "crit"].index, default="ok"),
        "temperature": s["cpu"]["temperature"], "temperature_level": s["cpu"]["temperature_level"],
        "uptime_seconds": s["uptime_seconds"],
    }
