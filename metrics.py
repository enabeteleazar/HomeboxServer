"""Historique des mesures du serveur.

Un collecteur (thread lancé au démarrage du dashboard) prend une mesure toutes
les SAMPLE_SECONDS secondes et l'enregistre dans <SRV_ROOT>/.data/metrics.db :

- samples   : mesures détaillées, gardées RAW_DAYS jours ;
- hourly    : moyenne et pic par heure, gardés HOURLY_DAYS jours ;
- disk_usage: remplissage de chaque partition, une ligne par heure (DISK_DAYS jours).

Le collecteur a ses propres compteurs (CPU, disque, réseau) : il ne fausse pas
les valeurs affichées en direct par la page Système, et inversement.
Un verrou de fichier garantit un seul collecteur, même si le dashboard est
lancé deux fois (service + `make dashboard`).
"""
from __future__ import annotations

import fcntl
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import psutil

from srvctl.srvctl import get_root

SAMPLE_SECONDS = 15
RAW_DAYS = 7
HOURLY_DAYS = 90
DISK_DAYS = 365

# Colonnes mesurées : (nom, description)
FIELDS = [
    "cpu", "mem", "swap", "load1", "temp",
    "disk_read", "disk_write", "disk_busy",
    "net_rx", "net_tx",
]
# Colonnes dont on garde aussi le pic horaire
PEAK_FIELDS = ["cpu", "mem", "temp", "disk_busy", "disk_read", "disk_write", "net_rx", "net_tx"]

RANGES = {
    "1h": 3600, "6h": 6 * 3600, "24h": 86400,
    "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400,
}
MAX_POINTS = 240

PHYSICAL_NICS = ("en", "eth", "wl", "wlan")  # tailscale0 passe déjà par ces interfaces


def db_path() -> Path:
    return get_root() / ".data" / "metrics.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    cols = ", ".join(f"{f} REAL" for f in FIELDS)
    peaks = ", ".join(f"{f}_max REAL" for f in PEAK_FIELDS)
    with connect() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, {cols})")
        conn.execute(f"CREATE TABLE IF NOT EXISTS hourly (ts INTEGER PRIMARY KEY, n INTEGER, {cols}, {peaks})")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS disk_usage ("
            "ts INTEGER, mount TEXT, used INTEGER, total INTEGER, percent REAL, PRIMARY KEY (ts, mount))"
        )


# --- mesure ------------------------------------------------------------------

class Sampler:
    """Compteurs propres au collecteur : chaque mesure couvre l'intervalle écoulé."""

    def __init__(self):
        self.prev_time = None
        self.prev_cpu = None
        self.prev_disk = None
        self.prev_net = None

    @staticmethod
    def _disk_totals() -> tuple[float, float, float]:
        read = write = busy = 0.0
        counters = psutil.disk_io_counters(perdisk=True) or {}
        for name, c in counters.items():
            # disques entiers seulement (sda, nvme0n1, vda…), pas les partitions
            if re.match(r"^(loop|ram|zram|dm-)", name):
                continue
            if re.match(r"^(sd|vd|hd|xvd)[a-z]+$", name) or re.match(r"^nvme\d+n\d+$", name) or re.match(r"^mmcblk\d+$", name):
                read += c.read_bytes
                write += c.write_bytes
                busy = max(busy, float(getattr(c, "busy_time", 0)))
        return read, write, busy

    @staticmethod
    def _net_totals() -> tuple[float, float]:
        rx = tx = 0.0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if name.startswith(PHYSICAL_NICS):
                rx += c.bytes_recv
                tx += c.bytes_sent
        return rx, tx

    @staticmethod
    def _cpu_temp() -> Optional[float]:
        try:
            sensors = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for entry in sensors.get("coretemp", []):
            if (entry.label or "").startswith("Package") and entry.current:
                return float(entry.current)
        for entries in sensors.values():
            for entry in entries:
                if entry.current and entry.current > 0:
                    return float(entry.current)
        return None

    def sample(self) -> Optional[dict]:
        now = time.monotonic()
        cpu = psutil.cpu_times()
        disk = self._disk_totals()
        net = self._net_totals()
        prev = (self.prev_time, self.prev_cpu, self.prev_disk, self.prev_net)
        self.prev_time, self.prev_cpu, self.prev_disk, self.prev_net = now, cpu, disk, net
        if prev[0] is None:
            return None  # première mesure : sert de référence

        elapsed = now - prev[0]
        total = sum(cpu) - sum(prev[1])
        idle = (cpu.idle + getattr(cpu, "iowait", 0)) - (prev[1].idle + getattr(prev[1], "iowait", 0))
        cpu_pct = max(0.0, min(100.0, 100.0 * (1 - idle / total))) if total > 0 else 0.0

        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        return {
            "cpu": round(cpu_pct, 1),
            "mem": round(vm.percent, 1),
            "swap": round(sw.percent, 1),
            "load1": round(psutil.getloadavg()[0], 2),
            "temp": self._cpu_temp(),
            "disk_read": max(0.0, (disk[0] - prev[2][0]) / elapsed),
            "disk_write": max(0.0, (disk[1] - prev[2][1]) / elapsed),
            "disk_busy": round(min(100.0, max(0.0, (disk[2] - prev[2][2]) / elapsed / 10)), 1),
            "net_rx": max(0.0, (net[0] - prev[3][0]) / elapsed),
            "net_tx": max(0.0, (net[1] - prev[3][1]) / elapsed),
        }


def _disk_usage_rows(ts: int) -> list[tuple]:
    rows, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "tmpfs", "overlay", "efivarfs") or part.mountpoint.startswith(("/snap", "/run")):
            continue
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        rows.append((ts, part.mountpoint, u.used, u.total, round(u.percent, 1)))
    return rows


def _aggregate_hour(conn: sqlite3.Connection, hour: int) -> None:
    avg = ", ".join(f"AVG({f})" for f in FIELDS)
    peaks = ", ".join(f"MAX({f})" for f in PEAK_FIELDS)
    row = conn.execute(
        f"SELECT COUNT(*), {avg}, {peaks} FROM samples WHERE ts >= ? AND ts < ?", (hour, hour + 3600)
    ).fetchone()
    if not row or not row[0]:
        return
    cols = ["n"] + FIELDS + [f"{f}_max" for f in PEAK_FIELDS]
    conn.execute(
        f"INSERT OR REPLACE INTO hourly (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
        (hour, *row),
    )


# --- collecteur --------------------------------------------------------------

_started = False
_lock_file = None


def start_collector() -> bool:
    """Lance le collecteur une seule fois par machine. Renvoie False si un
    autre processus collecte déjà."""
    global _started, _lock_file
    if _started:
        return True
    init_db()
    _lock_file = open(db_path().with_suffix(".lock"), "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False
    _started = True
    threading.Thread(target=_loop, name="metrics-collector", daemon=True).start()
    return True


def _loop() -> None:
    sampler = Sampler()
    sampler.sample()
    last_hour = int(time.time()) // 3600 * 3600
    while True:
        time.sleep(SAMPLE_SECONDS)
        try:
            values = sampler.sample()
            now = int(time.time())
            hour = now // 3600 * 3600
            with connect() as conn:
                if values:
                    conn.execute(
                        f"INSERT OR REPLACE INTO samples (ts, {', '.join(FIELDS)}) "
                        f"VALUES (?, {', '.join('?' * len(FIELDS))})",
                        (now, *(values[f] for f in FIELDS)),
                    )
                if hour != last_hour:
                    # heure écoulée : résumé horaire, remplissage des disques, ménage
                    _aggregate_hour(conn, last_hour)
                    conn.executemany("INSERT OR REPLACE INTO disk_usage VALUES (?, ?, ?, ?, ?)",
                                     _disk_usage_rows(last_hour))
                    conn.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_DAYS * 86400,))
                    conn.execute("DELETE FROM hourly WHERE ts < ?", (now - HOURLY_DAYS * 86400,))
                    conn.execute("DELETE FROM disk_usage WHERE ts < ?", (now - DISK_DAYS * 86400,))
                    last_hour = hour
        except Exception as exc:  # le collecteur ne doit jamais s'arrêter
            print(f"[metrics] mesure impossible : {exc}")


# --- lecture -----------------------------------------------------------------

def series(range_key: str) -> dict:
    """Points pour les graphiques, ramenés à MAX_POINTS au plus (moyenne + pic par tranche)."""
    span = RANGES.get(range_key, RANGES["24h"])
    now = int(time.time())
    start = now - span
    step = max(SAMPLE_SECONDS, span // MAX_POINTS)

    use_hourly = span > RAW_DAYS * 86400
    table = "hourly" if use_hourly else "samples"
    step = max(step, 3600) if use_hourly else step

    avg = ", ".join(f"AVG({f}) AS {f}" for f in FIELDS)
    if use_hourly:
        peaks = ", ".join(f"MAX({f}_max) AS {f}_max" for f in PEAK_FIELDS)
    else:
        peaks = ", ".join(f"MAX({f}) AS {f}_max" for f in PEAK_FIELDS)

    with connect() as conn:
        rows = conn.execute(
            f"SELECT (ts / ?) * ? AS bucket, {avg}, {peaks} FROM {table} "
            f"WHERE ts >= ? GROUP BY bucket ORDER BY bucket",
            (step, step, start),
        ).fetchall()
        oldest = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        oldest_hourly = conn.execute("SELECT MIN(ts) FROM hourly").fetchone()[0]

    points = []
    for r in rows:
        point = {"t": r["bucket"]}
        for f in FIELDS:
            point[f] = None if r[f] is None else round(r[f], 2)
        for f in PEAK_FIELDS:
            point[f"{f}_max"] = None if r[f"{f}_max"] is None else round(r[f"{f}_max"], 2)
        points.append(point)

    since = min(x for x in (oldest, oldest_hourly) if x) if (oldest or oldest_hourly) else None
    return {
        "range": range_key if range_key in RANGES else "24h",
        "start": start, "end": now, "step": step, "points": points,
        "aggregated": step > SAMPLE_SECONDS,
        "collecting_since": since,
        "summary": _summary(points),
        "disks": disk_trends(),
    }


def _summary(points: list[dict]) -> dict:
    result = {}
    for f in FIELDS:
        values = [p[f] for p in points if p[f] is not None]
        peaks = [p.get(f"{f}_max") for p in points if p.get(f"{f}_max") is not None]
        result[f] = {
            "last": values[-1] if values else None,
            "avg": round(sum(values) / len(values), 2) if values else None,
            "max": max(peaks or values) if (peaks or values) else None,
        }
    return result


def disk_trends(days: int = 30) -> list[dict]:
    """Remplissage actuel de chaque partition et, si elle grossit, date estimée
    où elle serait pleine (tendance linéaire sur `days` jours)."""
    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            "SELECT mount, ts, used, total, percent FROM disk_usage WHERE ts >= ? ORDER BY mount, ts",
            (now - days * 86400,),
        ).fetchall()

    by_mount: dict[str, list] = {}
    for r in rows:
        by_mount.setdefault(r["mount"], []).append(r)

    result = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "tmpfs", "overlay", "efivarfs") or part.mountpoint.startswith(("/snap", "/run")):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        history = by_mount.get(part.mountpoint, [])
        per_day = None
        full_in_days = None
        if len(history) >= 24 and history[-1]["ts"] - history[0]["ts"] >= 86400:
            # pente par moindres carrés, en octets par jour
            xs = [(h["ts"] - history[0]["ts"]) / 86400 for h in history]
            ys = [h["used"] for h in history]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            var = sum((x - mx) ** 2 for x in xs)
            if var > 0:
                per_day = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
                if per_day > 1024 * 1024:  # plus de 1 Mo/jour : tendance significative
                    full_in_days = int(u.free / per_day)
        result.append({
            "mount": part.mountpoint, "percent": round(u.percent, 1), "used": u.used, "total": u.total,
            "free": u.free, "per_day": per_day, "full_in_days": full_in_days,
            "history_days": round((history[-1]["ts"] - history[0]["ts"]) / 86400, 1) if len(history) > 1 else 0,
            "spark": [h["percent"] for h in history[-60:]],
        })
    seen, unique = set(), []
    for d in sorted(result, key=lambda d: (d["mount"] != "/", d["mount"])):
        if d["mount"] not in seen:
            seen.add(d["mount"])
            unique.append(d)
    return unique
