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


SMART_FILE = "/var/lib/homebox/smart.json"


def smart() -> dict:
    """Relevé SMART écrit toutes les heures par homebox-smart (dossier infra/)."""
    try:
        with open(SMART_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"available": False, "reason": "missing"}
    except (OSError, json.JSONDecodeError):
        return {"available": False, "reason": "unreadable"}
    age = int(time.time()) - int(data.get("generated_ts") or 0)
    data.update(available=True, age_seconds=age, stale=age > 3 * 3600)
    return data


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
        "smart": smart(),
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
        project = labels.get("com.docker.compose.project", "")
        # Un conteneur arrêté n'est un problème que s'il fait partie de
        # l'infrastructure HOMEBOX : ailleurs, c'est souvent un arrêt voulu.
        if health == "unhealthy" or state == "dead" or (state == "exited" and project == "homebox"):
            status_level = "crit"
        elif state in ("restarting", "paused") or health == "starting":
            status_level = "warn"
        elif state in ("exited", "created"):
            status_level = "off"
        else:
            status_level = "ok"
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
        "problems": sum(1 for c in containers if c["level"] in ("warn", "crit")),
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


# --- état public écrit par homebox-status (dossier infra/) ---------------

STATUS_FILE = "/var/lib/homebox/status.json"


def homebox_status() -> dict:
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"available": False}
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    age = int(time.time()) - int(data.get("generated_ts") or 0)
    data.update(available=True, age_seconds=age, stale=age > 45 * 60)
    return data


# --- projets : processus, consommation, temps de réponse --------------------

_proc_cache: dict[int, psutil.Process] = {}
_probe_cache: dict[int, tuple[float, dict]] = {}
PROBE_TTL = 20


def _listening_pids() -> dict[int, int]:
    """Port TCP en écoute → PID (visible seulement pour nos propres processus)."""
    result = {}
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.pid and conn.laddr:
                result.setdefault(conn.laddr.port, conn.pid)
    except (psutil.Error, OSError):
        pass
    return result


def _process_tree(pid: int) -> list[psutil.Process]:
    """Le processus et ses enfants, en réutilisant les objets déjà vus
    (cpu_percent a besoin de deux lectures sur le même objet)."""
    procs = []
    try:
        top = _proc_cache.get(pid) or psutil.Process(pid)
        _proc_cache[pid] = top
        procs.append(top)
        for child in top.children(recursive=True):
            procs.append(_proc_cache.setdefault(child.pid, child))
    except psutil.Error:
        _proc_cache.pop(pid, None)
    return procs


def _probe_http(port: int) -> dict:
    cached = _probe_cache.get(port)
    if cached and time.time() - cached[0] < PROBE_TTL:
        return cached[1]
    import http.client
    started = time.monotonic()
    result: dict
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/", headers={"User-Agent": "srv-dashboard/monitor"})
        response = conn.getresponse()
        response.read(65536)
        conn.close()
        result = {"ok": response.status < 500, "status": response.status,
                  "ms": round((time.monotonic() - started) * 1000)}
    except (OSError, http.client.HTTPException) as exc:
        result = {"ok": False, "status": None, "ms": None, "error": str(exc)[:120]}
    result["level"] = ("crit" if not result["ok"] else
                       "warn" if (result["ms"] or 0) > 1500 else "ok")
    _probe_cache[port] = (time.time(), result)
    return result


def project_runtime(port: Optional[int], pids: Optional[dict[int, int]] = None) -> Optional[dict]:
    if not port:
        return None
    pids = _listening_pids() if pids is None else pids
    pid = pids.get(int(port))
    runtime: dict = {"pid": pid, "cpu": None, "memory": None, "processes": 0, "started": None, "command": ""}
    if pid:
        procs = _process_tree(pid)
        cpu = mem = 0.0
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                mem += p.memory_info().rss
            except psutil.Error:
                continue
        runtime.update(cpu=round(cpu, 1), memory=int(mem), processes=len(procs))
        try:
            runtime["started"] = datetime.fromtimestamp(procs[0].create_time(), timezone.utc).isoformat()
            runtime["command"] = " ".join(procs[0].cmdline())[:160]
        except (psutil.Error, IndexError):
            pass
    runtime["http"] = _probe_http(int(port))
    return runtime


def projects_runtime(projects: list[dict]) -> dict[str, dict]:
    """Consommation et temps de réponse des projets en ligne (sondes en parallèle)."""
    from concurrent.futures import ThreadPoolExecutor
    pids = _listening_pids()
    running = [p for p in projects if p.get("up") and p.get("port")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda p: (p["name"], project_runtime(p["port"], pids)), running)
    return dict(results)


# --- vue d'ensemble : un voyant par domaine ---------------------------------

def _worst(levels: list[str]) -> str:
    order = ["ok", "off", "unknown", "warn", "crit"]
    return max(levels or ["ok"], key=lambda l: order.index(l) if l in order else 0)


def overview(projects: list[dict]) -> dict:
    sysinfo = system()
    svc = services()
    dock = docker()
    st = homebox_status()
    tiles = []

    groups = {
        "services": "services", "docker": "services", "projects": "services",
        "backup": "protection", "offsite": "protection", "firewall": "protection", "ssh": "protection",
        "certs": "protection",
    }

    def tile(key, label, level, value, note, href):
        tiles.append({"key": key, "label": label, "level": level, "value": value, "note": note,
                      "href": href, "group": groups.get(key, "machine")})

    cpu, mem = sysinfo["cpu"], sysinfo["memory"]
    tile("cpu", "Processeur", cpu["level"], f"{cpu['percent']:.0f} %", f"charge {cpu['load'][0]}", "/system")
    tile("mem", "Mémoire", mem["level"], f"{mem['percent']:.0f} %", "hors cache", "/system")
    fullest = max(sysinfo["disks"], key=lambda d: d["percent"], default=None)
    if fullest:
        tile("disk", "Disque", fullest["level"], f"{fullest['percent']:.0f} %", f"{fullest['mount']} le plus rempli", "/history")
    if cpu["temperature"] is not None:
        tile("temp", "Température", cpu["temperature_level"], f"{cpu['temperature']:.0f} °C", "processeur", "/system")

    sm = sysinfo.get("smart") or {}
    if sm.get("available"):
        tile("smart", "Santé du disque", "warn" if sm.get("stale") and sm["status"] == "ok" else sm["status"],
             {"ok": "Bonne", "warn": "À surveiller", "crit": "En danger"}.get(sm["status"], "?"),
             "relevé SMART " + ("ancien" if sm.get("stale") else "récent"), "/system")
    else:
        tile("smart", "Santé du disque", "unknown", "?", "relevé SMART non installé", "/system")

    svc_bad = [s for s in svc["services"] if s["level"] in ("crit", "warn")]
    tile("services", "Services", "crit" if svc["failed"] or any(s["level"] == "crit" for s in svc_bad) else
         ("warn" if svc_bad or svc["problems"] else "ok"),
         f"{sum(1 for s in svc['services'] if s['active'] == 'active')}/{len(svc['services'])}",
         f"{svc['problems']} problème(s)" if svc["problems"] else "tous actifs", "/services")

    if dock.get("available"):
        tile("docker", "Docker", "warn" if dock["problems"] else "ok", f"{dock['running']}/{dock['total']}",
             f"{dock['problems']} à regarder" if dock["problems"] else "conteneurs en marche", "/docker")
    else:
        tile("docker", "Docker", "crit", "—", "Docker ne répond pas", "/docker")

    up = sum(1 for p in projects if p.get("up"))
    tile("projects", "Projets en ligne", "ok", f"{up}/{len(projects)}", "serveurs de dev et sites", "/")

    upd = sysinfo.get("updates")
    if upd is not None:
        lvl = "crit" if sysinfo["reboot_required"] else ("warn" if upd["security"] else ("off" if upd["count"] else "ok"))
        tile("updates", "Mises à jour", lvl, str(upd["count"]),
             ("redémarrage requis · " if sysinfo["reboot_required"] else "") +
             (f"dont {upd['security']} de sécurité" if upd["security"] else "en attente"), "/system")

    if st.get("available"):
        items = st["backups"]["items"]
        if items:
            age_h = (time.time() - datetime.fromisoformat(items[0]["time"]).timestamp()) / 3600
            lvl = "ok" if age_h <= 26 else ("warn" if age_h <= 50 else "crit")
            tile("backup", "Dernière sauvegarde", lvl, f"il y a {int(age_h)} h" if age_h >= 1 else "à l'instant",
                 f"{st['backups']['count']} conservées", "/backups")
        else:
            tile("backup", "Dernière sauvegarde", "crit", "aucune", "aucune sauvegarde locale", "/backups")
        off = st["offsite"]
        if not off["configured"]:
            tile("offsite", "Copie externe", "warn", "non configurée", "une panne de disque emporterait tout", "/backups")
        else:
            last = off.get("last")
            age_h = (time.time() - datetime.fromisoformat(last).timestamp()) / 3600 if last else None
            tile("offsite", "Copie externe", "ok" if age_h is not None and age_h <= 26 else "crit",
                 off["kind"] or "configurée", f"il y a {int(age_h)} h" if age_h is not None else "jamais envoyée", "/backups")
        sec = st["security"]
        tile("firewall", "Pare-feu", "ok" if sec["ufw_active"] else "crit", "actif" if sec["ufw_active"] else "inactif",
             f"{sec['fail2ban_banned']} adresse(s) bannie(s)", "/firewall")
        ssh_lvl = "crit" if sec["ssh_root_login"] else ("warn" if sec["ssh_password_auth"] else "ok")
        tile("ssh", "SSH", ssh_lvl, "clés uniquement" if not sec["ssh_password_auth"] else "mots de passe acceptés",
             "connexion root " + ("autorisée" if sec["ssh_root_login"] else "interdite"), "/overview#securite")
        certs = st.get("certificates") or []
        if certs:
            soonest = min((c for c in certs if c.get("ok")), key=lambda c: c["days_left"], default=None)
            bad = [c for c in certs if not c.get("ok")]
            lvl = "crit" if bad or (soonest and soonest["days_left"] < 7) else ("warn" if soonest and soonest["days_left"] < 14 else "ok")
            tile("certs", "Certificats HTTPS", lvl, f"{len(certs)} site(s)",
                 f"le plus proche expire dans {soonest['days_left']} j" if soonest else "illisibles", "/overview#sites")

    group_titles = [("machine", "Machine"), ("services", "Services et projets"), ("protection", "Protection")]
    return {
        "tiles": tiles,
        "groups": [{"key": k, "title": t, "tiles": [x for x in tiles if x["group"] == k]}
                   for k, t in group_titles if any(x["group"] == k for x in tiles)],
        "status": st,
        "level": _worst([t["level"] for t in tiles]),
        "problems": sum(1 for t in tiles if t["level"] in ("warn", "crit")),
    }
