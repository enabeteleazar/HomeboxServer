"""Dashboard /srv — interface web.

Lancement : `make dashboard` (uvicorn main:app depuis le dossier du dépôt).
L'API JSON pour Neron vit dans api.py, la logique commune dans services.py.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

# srvctl vit à côté de ce fichier : on s'assure que le dossier du
# dépôt est sur sys.path quel que soit le cwd (uvicorn, make, cron…).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import firewall  # noqa: E402
import monitor  # noqa: E402
import services  # noqa: E402
import webauth  # noqa: E402
from api import API_VERSION, router as public_router, secured as secured_router  # noqa: E402
from auth import ENV_KEY, is_configured  # noqa: E402
from srvctl import journal  # noqa: E402
from srvctl.srvctl import load_registry  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    journal.init_db()
    stale = journal.recover_stale()
    if stale:
        print(f"[dashboard] {stale} action(s) interrompue(s) au redémarrage, marquée(s) en erreur.")
    yield


app = FastAPI(
    title="srv dashboard API",
    version=API_VERSION,
    summary="Pilotage des projets hébergés sous /srv (homebox) — conçue pour être appelée par Neron.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(public_router)
app.include_router(secured_router)


# --- authentification de l'interface web ----------------------------------
#
# Tout est protégé par la session, sauf : la page de connexion, les fichiers
# statiques et l'API /api/v1 (qui a son propre jeton Bearer, voir auth.py).

PUBLIC_PATHS = {"/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/", "/api/v1/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _same_origin(request: Request) -> bool:
    """Protection CSRF en plus du cookie SameSite=Strict : un formulaire
    envoyé depuis un autre site porte un Origin (ou Referer) étranger."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # navigateurs anciens / outils en ligne de commande
    return urlsplit(source).netloc == request.headers.get("host", "")


def _safe_next(target: Optional[str]) -> str:
    if target and target.startswith("/") and not target.startswith("//") and target != "/login":
        return target
    return "/"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "inconnu"


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path

    if not _is_public(path):
        if not webauth.is_configured():
            response = HTMLResponse(
                templates.get_template("setup_password.html").render(api_version=API_VERSION),
                status_code=503,
            )
        elif not webauth.session_valid(request.cookies.get(webauth.COOKIE_NAME)):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                target = path + (f"?{request.url.query}" if request.url.query else "")
                response = RedirectResponse(f"/login?next={quote(target)}", status_code=303)
            else:
                response = JSONResponse({"detail": "Connexion requise."}, status_code=401)
        elif request.method in UNSAFE_METHODS and not _same_origin(request):
            response = PlainTextResponse("Requête refusée (origine différente).", status_code=403)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


# --- filtres d'affichage --------------------------------------------------

def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_datetime(value: Optional[str]) -> str:
    dt = _parse(value)
    return dt.astimezone().strftime("%d/%m/%Y %H:%M") if dt else "—"


def fmt_time(value: Optional[str]) -> str:
    dt = _parse(value)
    return dt.astimezone().strftime("%H:%M") if dt else "—"


def fmt_ago(value: Optional[str]) -> str:
    dt = _parse(value)
    if not dt:
        return "—"
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "à l'instant"
    if seconds < 3600:
        return f"il y a {seconds // 60} min"
    if seconds < 86400:
        hours = seconds // 3600
        return f"il y a {hours} h"
    days = seconds // 86400
    if days == 1:
        return "hier"
    if days < 30:
        return f"il y a {days} j"
    return fmt_datetime(value)


def fmt_duration(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest:02d} s"


def fmt_bytes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    value = float(value)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(value) < 1024 or unit == "To":
            text = f"{value:.0f}" if unit == "o" or value >= 100 else f"{value:.1f}"
            return f"{text.replace('.', ',')} {unit}"
        value /= 1024
    return "—"


def fmt_rate(value: Optional[float]) -> str:
    return "—" if value is None else f"{fmt_bytes(value)}/s"


def fmt_uptime(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} j {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def fmt_until(value: Optional[str]) -> str:
    dt = _parse(value)
    if not dt:
        return "—"
    seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "imminent"
    if seconds < 3600:
        return f"dans {max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"dans {seconds // 3600} h"
    return f"dans {seconds // 86400} j"


def fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.0f} %" if value >= 10 else f"{value:.1f} %".replace(".", ",")


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["rate"] = fmt_rate
templates.env.filters["uptime"] = fmt_uptime
templates.env.filters["until"] = fmt_until
templates.env.filters["pct"] = fmt_pct
templates.env.filters["dt"] = fmt_datetime
templates.env.filters["time"] = fmt_time
templates.env.filters["ago"] = fmt_ago
templates.env.filters["dur"] = fmt_duration


# --- helpers --------------------------------------------------------------

def render(request: Request, template: str, nav: str, **context):
    params = request.query_params
    base = {
        "nav": nav,
        "flash": params.get("msg"),
        "flash_kind": params.get("kind", "ok"),
        "api_configured": is_configured(),
        "api_version": API_VERSION,
        "actions": services.ACTIONS,
        "roots": services.ROOTS,
        "types": services.TYPES,
        "statuses": services.STATUSES,
        "priorities": services.PRIORITIES,
        "status_labels": services.STATUS_LABELS,
        "priority_labels": services.PRIORITY_LABELS,
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base)


def back(url: str, message: str, kind: str = "ok") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={quote(message)}&kind={kind}", status_code=303)


# --- connexion ------------------------------------------------------------

def _login_page(request: Request, next_url: str, error: Optional[str] = None, status_code: int = 200):
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next_url, "error": error, "configured": webauth.is_configured(), "api_version": API_VERSION},
        status_code=status_code,
    )


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: Optional[str] = None):
    if webauth.is_configured() and webauth.session_valid(request.cookies.get(webauth.COOKIE_NAME)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, _safe_next(next))


@app.post("/login", include_in_schema=False)
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    next_url = _safe_next(next)
    if not webauth.is_configured():
        return _login_page(request, next_url, status_code=503)
    if not _same_origin(request):
        return _login_page(request, next_url, "Requête refusée (origine différente).", 403)

    ip = _client_ip(request)
    wait = webauth.retry_after(ip)
    if wait:
        minutes = max(1, round(wait / 60))
        return _login_page(request, next_url, f"Trop de tentatives : réessaie dans {minutes} min.", 429)

    if not webauth.check_login(ip, password):
        print(f"[dashboard] échec de connexion depuis {ip}")
        return _login_page(request, next_url, "Mot de passe incorrect.", 401)

    print(f"[dashboard] connexion réussie depuis {ip}")
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        webauth.COOKIE_NAME, webauth.new_session_token(),
        max_age=webauth.SESSION_SECONDS, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path="/",
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(webauth.COOKIE_NAME, path="/")
    return response


# --- pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def overview(request: Request):
    projects = services.list_projects(host=request.url.hostname)
    return render(
        request, "overview.html", nav="overview",
        projects=projects,
        summary=services.summary(),
        activity=journal.list_activity(limit=8),
    )


@app.get("/projects/{name}", response_class=HTMLResponse, include_in_schema=False)
def project_page(request: Request, name: str):
    try:
        project = services.get_project(name, host=request.url.hostname)
    except services.ServiceError as exc:
        return back("/", exc.message, "error")
    return render(
        request, "project.html", nav="overview", project=project,
        logs=services.read_logs(name, lines=200),
    )


@app.get("/activity", response_class=HTMLResponse, include_in_schema=False)
def activity_page(request: Request, project: Optional[str] = None, status: Optional[str] = None):
    return render(
        request, "activity.html", nav="activity",
        entries=journal.list_activity(project=project, status=status, limit=200),
        all_projects=list(load_registry().keys()),
        selected_project=project, selected_status=status,
    )


@app.get("/activity/{activity_id}", response_class=HTMLResponse, include_in_schema=False)
def activity_detail_page(request: Request, activity_id: int):
    entry = journal.get_activity(activity_id)
    if entry is None:
        return back("/activity", f"Activité {activity_id} inconnue", "error")
    return render(request, "activity_detail.html", nav="activity", entry=entry)


@app.get("/deployments", response_class=HTMLResponse, include_in_schema=False)
def deployments_page(request: Request, project: Optional[str] = None, environment: Optional[str] = None):
    return render(
        request, "deployments.html", nav="deployments",
        deployments=journal.list_deployments(project=project, environment=environment, limit=200),
        selected_project=project, selected_environment=environment,
    )


# --- serveur : système, Docker, services (rafraîchis en direct) -------------
#
# Chaque page charge son contenu une première fois, puis app.js recharge le
# fragment « /…/live » toutes les quelques secondes (en pause si l'onglet
# est caché).

LIVE_PAGES = {
    "system": ("system.html", "_system_live.html", monitor.system, 5),
    "docker": ("docker.html", "_docker_live.html", monitor.docker, 10),
    "services": ("services.html", "_services_live.html", monitor.services, 15),
}


def _live_page(request: Request, key: str):
    page, _, collect, interval = LIVE_PAGES[key]
    return render(request, page, nav=key, data=collect(), live_interval=interval * 1000)


def _live_fragment(request: Request, key: str):
    _, fragment, collect, _ = LIVE_PAGES[key]
    return templates.TemplateResponse(request, fragment, {"data": collect()})


@app.get("/system", response_class=HTMLResponse, include_in_schema=False)
def system_page(request: Request):
    return _live_page(request, "system")


@app.get("/system/live", response_class=HTMLResponse, include_in_schema=False)
def system_live(request: Request):
    return _live_fragment(request, "system")


@app.get("/docker", response_class=HTMLResponse, include_in_schema=False)
def docker_page(request: Request):
    return _live_page(request, "docker")


@app.get("/docker/live", response_class=HTMLResponse, include_in_schema=False)
def docker_live(request: Request):
    return _live_fragment(request, "docker")


@app.get("/services", response_class=HTMLResponse, include_in_schema=False)
def services_page(request: Request):
    return _live_page(request, "services")


@app.get("/services/live", response_class=HTMLResponse, include_in_schema=False)
def services_live(request: Request):
    return _live_fragment(request, "services")


@app.get("/firewall", response_class=HTMLResponse, include_in_schema=False)
def firewall_page(request: Request, preview: Optional[int] = None):
    data = firewall.overview()
    policy_preview = None
    if preview and data["available"]:
        try:
            policy_preview = firewall.apply_policy(dry_run=True).get("message", "")
        except firewall.FirewallError as exc:
            policy_preview = exc.message
    return render(request, "firewall.html", nav="firewall", fw=data, policy_preview=policy_preview,
                  exposure_labels=firewall.EXPOSURE_LABELS)


def _firewall_result(result: dict, success: str) -> RedirectResponse:
    if result.get("ok"):
        return back("/firewall", success)
    return back("/firewall", result.get("message") or "Opération refusée", "error")


@app.post("/firewall/rule", include_in_schema=False)
def firewall_add(
    action: str = Form("allow"),
    port: str = Form(""),
    proto: str = Form("tcp"),
    source: str = Form("any"),
    custom_source: str = Form(""),
    comment: str = Form(""),
):
    iface = ""
    if source == "lan":
        source = firewall.lan_network() or ""
        if not source:
            return back("/firewall", "Réseau local introuvable", "error")
    elif source == "tailscale":
        source, iface = "any", "tailscale0"
    elif source == "custom":
        source = custom_source.strip()
    elif source != "any":
        return back("/firewall", "Source invalide", "error")
    try:
        result = firewall.add_rule(action, port.strip(), proto, source, iface, comment.strip())
    except firewall.FirewallError as exc:
        return back("/firewall", exc.message, "error")
    return _firewall_result(result, "Règle ajoutée")


@app.post("/firewall/delete", include_in_schema=False)
def firewall_delete(number: int = Form(...), expect: str = Form(...)):
    try:
        result = firewall.delete_rule(number, expect)
    except firewall.FirewallError as exc:
        return back("/firewall", exc.message, "error")
    return _firewall_result(result, "Règle supprimée")


@app.post("/firewall/state", include_in_schema=False)
def firewall_state(enabled: str = Form(...), confirm: str = Form("")):
    turn_on = enabled == "on"
    if not turn_on and confirm != "DÉSACTIVER":
        return back("/firewall", "Désactivation annulée : tape DÉSACTIVER pour confirmer", "error")
    try:
        result = firewall.set_enabled(turn_on)
    except firewall.FirewallError as exc:
        return back("/firewall", exc.message, "error")
    return _firewall_result(result, "Pare-feu activé" if turn_on else "Pare-feu désactivé")


@app.post("/firewall/policy", include_in_schema=False)
def firewall_policy():
    try:
        result = firewall.apply_policy(dry_run=False)
    except firewall.FirewallError as exc:
        return back("/firewall", exc.message, "error")
    return _firewall_result(result, "Politique HOMEBOX appliquée")


@app.get("/api", response_class=HTMLResponse, include_in_schema=False)
def api_page(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return render(
        request, "api.html", nav="api",
        base_url=base_url, api_base=f"{base_url}/api/v1",
        env_key=ENV_KEY, api_version=API_VERSION,
    )


# --- actions (formulaires) ------------------------------------------------

@app.post("/projects/{name}/action", include_in_schema=False)
def action_form(name: str, action: str = Form(...)):
    try:
        services.run_action(name, action, source="dashboard")
    except services.ServiceError as exc:
        return back(f"/projects/{quote(name)}", exc.message, "error")
    label = services.ACTIONS[action]["label"]
    return back(f"/projects/{quote(name)}", f"{label} : lancé sur {name}")


@app.post("/projects/{name}/meta", include_in_schema=False)
def meta_form(
    name: str,
    status: str = Form(...),
    priority: str = Form(...),
    description: str = Form(""),
    notes: str = Form(""),
):
    try:
        services.update_project(
            name, {"status": status, "priority": priority, "description": description, "notes": notes}
        )
    except services.ServiceError as exc:
        return back(f"/projects/{quote(name)}", exc.message, "error")
    return back(f"/projects/{quote(name)}", "Suivi mis à jour")


@app.post("/projects/new", include_in_schema=False)
def create_form(
    name: str = Form(...),
    root: str = Form("projects"),
    type: str = Form("web"),
    description: str = Form(""),
    priority: str = Form("normal"),
    visibility: str = Form("public"),
    git: Optional[str] = Form(None),
    vercel: Optional[str] = Form(None),
):
    try:
        services.create_project(
            name=name, root=root, type=type, description=description, priority=priority,
            visibility=visibility, git=bool(git), vercel=bool(vercel), source="dashboard",
        )
    except services.ServiceError as exc:
        return back("/", exc.message, "error")
    return back(f"/projects/{quote(name.strip())}", f"{name.strip()} créé")


@app.post("/sync", include_in_schema=False)
def sync_form():
    activity = services.sync(source="dashboard", wait=True)
    ok = activity.get("status") == "ok"
    return back("/", "Sync terminé" if ok else "Sync en échec — voir le journal", "ok" if ok else "error")


# --- petites routes de service -------------------------------------------

@app.get("/ui/state", include_in_schema=False)
def ui_state():
    """Sondée par la page toutes les quelques secondes : dès qu'une action
    se termine, la page se recharge d'elle-même."""
    running = journal.running()
    return JSONResponse(
        {
            "running": [
                {"id": r["id"], "project": r["project"], "action": r["action"], "started_at": r["started_at"]}
                for r in running
            ],
            "count": len(running),
        }
    )


@app.get("/logs/{name}", response_class=PlainTextResponse, include_in_schema=False)
def logs_plain(name: str, lines: int = 300):
    try:
        content = services.read_logs(name, lines=lines)
    except services.ServiceError as exc:
        return PlainTextResponse(exc.message, status_code=exc.status_code)
    return PlainTextResponse(content or f"Pas de log pour « {name} » — le projet n'a peut-être jamais été démarré.")
