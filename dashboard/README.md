# srv-dashboard — gestion des projets de homebox

Outillage complet du serveur `/srv` : le Makefile, la couche Python `srvctl`, le dashboard web et l'API REST que Neron appelle. Ce dépôt est autonome — tout le code est ici, seules les données du serveur restent dehors.

- **`Makefile`** — les commandes historiques (`make list`, `make dev <projet>`, `make deploy <projet>`…). C'est lui qui fait le vrai travail ; le dashboard et l'API ne font que l'appeler.
- **`srvctl/`** — le registre des projets (`srvctl.py`), la détection des nouveaux dossiers, le type de projet, le port libre, le `.env`, `git init` + `gh repo create` (org NABET), `vercel link`, la génération de `.projects.mk` et le journal SQLite.
- **`main.py` / `api.py` / `services.py` / `auth.py` / `schemas.py`** — l'interface web et l'API `/api/v1`, avec `templates/` et `static/`.
- **`clients/`** — le client Python à déposer dans Neron.
- **`systemd/`** — l'unité qui fait tourner le dashboard en permanence.

## Comment ça s'installe

Ce dossier est la partie **dashboard** du dépôt HomeboxServer (`/srv/HomeboxServer/dashboard`), à côté de la partie **infra/** (scripts root, sauvegardes, pare-feu) qu'il affiche et pilote. Un lien symbolique met son Makefile à la racine de `/srv` pour garder l'habitude `cd /srv && make list` :

```
/srv/
├── Makefile -> HomeboxServer/dashboard/Makefile   lien symbolique
├── HomeboxServer/                                 ce dépôt
│   ├── dashboard/                                 l'application (ici)
│   │   └── srvctl/projects.json                   registre des projets (données, hors dépôt)
│   └── infra/                                     scripts root, outils homebox-*
├── .env                                           ports + DASHBOARD_API_TOKEN (jamais versionné)
├── .projects.mk .data/ .logs/                     généré : listes Make, journal SQLite, logs
└── Projects/ Neron/                               les projets, chacun avec son propre dépôt git
```

Sur une machine neuve, voir le README à la racine du dépôt ; pour le dashboard seul :

```bash
cd /srv && git clone git@github.com:enabeteleazar/HomeboxServer.git
ln -s HomeboxServer/dashboard/Makefile Makefile
python3 -m venv HomeboxServer/dashboard/.venv
HomeboxServer/dashboard/.venv/bin/pip install -r HomeboxServer/dashboard/requirements.txt
make sync            # scanne Projects/ personal/, met à jour srvctl/projects.json
make api-token       # jeton de l'API pour Neron
make dashboard-password
```

Ailleurs que dans `/srv`, une seule variable suffit — le Makefile et `srvctl` lisent la même : `SRV_ROOT=/opt/srv make list` (et `Environment=SRV_ROOT=/opt/srv` dans l'unité systemd). Le registre, lui, suit le dépôt ; `SRV_REGISTRY=/chemin/projects.json` permet de le placer ailleurs (tests, déploiement particulier). Sous Linux la casse compte : la racine logique `neron` correspond au dossier `/srv/Neron`.

Première installation sur un `/srv` déjà peuplé : `make sync` puis `python3 -m srvctl.cli import-env` récupère les ports déjà en service depuis le `.env` existant. Les champs de suivi (`status`, `priority`, `description`, `notes`) sont facultatifs dans le registre : une entrée qui ne les contient pas reste valide, le projet démarre en « actif / priorité normale ».

À chaque `make sync`, un projet dont le dossier n'existe plus est retiré du registre (et ses variables `<NOM>_PORT` / `<NOM>_TYPE` du `.env`) ; un projet déplacé d'une racine à l'autre voit simplement son chemin mis à jour. Rien n'est supprimé sur le disque ni sur GitHub/Vercel. Garde-fous : si la racine entière est introuvable (disque non monté), ses projets sont conservés ; `make sync ARGS=--keep-missing` désactive le retrait.

## Le registre des projets

Un seul fichier, `srvctl/projects.json`, lu et écrit par `srvctl/srvctl.py` (`load_registry` / `save_registry`, un dictionnaire nom → entrée). Le dashboard, l'API, le Makefile (via `.projects.mk`) et `srvctl.cli` partent tous de lui ; il n'existe plus d'autre `projects.json`. Le fichier contient des données propres au serveur (chemins, ports) : il est dans `.gitignore`, et un dépôt fraîchement cloné démarre avec un registre vide.

Chaque entrée :

| Champ | Rôle |
|---|---|
| `name`, `root`, `path` | nom, racine logique (`projects` / `personal` / `neron`) et chemin complet du dossier |
| `type` | `web`, `webapp` ou `custom` (voir plus bas) |
| `port`, `start` | port attribué et commande de démarrage du service |
| `vercel`, `vercel_enabled`, `github_repo`, `git`, `visibility` | liens Vercel et GitHub |
| `status`, `priority`, `description`, `notes` | suivi (facultatifs) |

Les trois types, et ce que fait le Makefile de chacun :

| Type | Projet | Listes générées dans `.projects.mk` |
|---|---|---|
| `web` | site statique, servi par `npx serve` | `STATIC_PROJECTS` |
| `webapp` | application avec serveur de dev (Vite), lancée par `pnpm dev` | `WEB_PROJECTS` |
| `custom` | cycle de vie sur mesure, commande `start` écrite à la main | `WEB_PROJECTS` et `CUSTOM_WEB_PROJECTS` |

Avant la migration au registre unique, ces types s'appelaient `static` et `vite` et vivaient dans `/srv/projects.json`, qui n'est plus lu (on peut le garder en archive).

## Ajouter un projet

Trois chemins, tous équivalents — ils finissent au même endroit :

1. **Dashboard** : bouton « Nouveau projet » (nom, dossier, type, priorité, repo GitHub, Vercel).
2. **Neron / API** : `POST /api/v1/projects`.
3. **Ligne de commande** : déposer le dossier dans `/srv/projects/` puis `make sync`.

Dans les trois cas : type deviné (`web` sans `package.json`, `webapp` si vite est détecté, `custom` si un script `dev` / `build` / `serve` existe sans vite), premier port libre attribué (plage 5180-5299, 8000-8099 pour la racine `neron`, le port du dashboard est exclu), clés `<PROJET>_PORT` / `<PROJET>_TYPE` ajoutées dans `.env`, `.projects.mk` régénéré, puis `git init` + `gh repo create NABET/<nom>` et `vercel link` selon les options. Ensuite `make dev`, `make deploy`… fonctionnent immédiatement, sans toucher au Makefile.

Un projet au cycle de vie sur mesure (`wwdc26Bingo`) est détecté `custom`, mais il faut renseigner son champ `start` dans le registre, et `oraneCoatch` garde sa branche dédiée dans `dev-project` / `start-project` : l'automatisation ne peut pas deviner une commande de démarrage arbitraire. `make sync` et `add` le signalent d'une ligne `! type custom`.

## Dashboard

```bash
make dashboard                       # http://0.0.0.0:5199
DASHBOARD_PORT=5195 make dashboard
```

Quatre pages :

- **Projets** — compteurs (actifs, en ligne, déploiements et échecs sur 7 jours), graphique des actions par jour sur 14 jours (bleu = réussies, orange = échecs, avec infobulle et table de données dépliable), flux des dernières actions, et le tableau des projets avec recherche et filtres par statut / dossier.
- **Fiche projet** — actions groupées (service, build, git, Vercel — « Mettre en production » demande confirmation), journal du projet, historique des déploiements avec leurs URLs, commits récents, logs du service, et le bloc « Suivi » éditable : statut (actif / en pause / terminé / idée), priorité, description, notes.
- **Activité** — le journal complet, filtrable par projet et par résultat ; chaque ligne ouvre la sortie complète de la commande.
- **Déploiements** — tous les deploy preview/production avec l'URL Vercel retournée.

Détails d'usage : les actions longues (build, deploy) tournent en arrière-plan — la page reste utilisable et se rafraîchit toute seule à la fin. Un seul job à la fois par projet (une deuxième action sur le même projet est refusée). Thème clair/sombre suivant le système, avec bascule manuelle mémorisée dans le navigateur. Pas d'authentification sur l'interface web : l'accès est déjà limité par Tailscale.

Ce qui n'apparaît pas dans le journal : les commandes lancées directement en SSH (`make deploy duoLingo`). Seul ce qui passe par le dashboard ou l'API est tracé.

### Suivi du serveur

Pages rafraîchies en direct (en pause quand l'onglet est caché) :

| Page | Contenu | Rafraîchissement |
|---|---|---|
| Vue d'ensemble | un voyant par domaine (CPU, mémoire, disque, température, santé SMART, services, Docker, projets, mises à jour, dernière sauvegarde, copie externe, pare-feu, SSH, certificats HTTPS), alertes en cours, sécurité, sites HTTPS | 15 s |
| Sauvegardes | dernière sauvegarde, liste et contenu, copie externe, test de restauration ; boutons « Sauvegarder maintenant » et « Actualiser l'état » | à la demande |
| Système | CPU (par cœur, charge, fréquence), mémoire et swap, partitions, activité disque, réseau par interface, températures, processus les plus gourmands, mises à jour apt en attente, redémarrage requis | 5 s |
| Docker | conteneurs par projet Compose (état, santé, CPU, mémoire, ports), espace disque Docker, volumes | 10 s |
| Services | services systemd essentiels, tâches planifiées (dernier/prochain passage, résultat), unités en échec | 15 s |
| Pare-feu | état UFW, exposition réelle de chaque port, règles, fail2ban | à la demande |
| Caddy | service, validité de la configuration, sites publiés (cible, projet, service en ligne, certificat HTTPS), publication d'un projet / d'un port / d'un dossier statique, retrait, e-mail Let's Encrypt, journal | à la demande |
| Historique | graphiques CPU, mémoire, température, charge, débit et occupation du disque, réseau (1 h à 90 jours, moyenne et pic) ; tendance de remplissage des partitions et date estimée où elles seraient pleines | 1 min |

**Sauvegardes, alertes, sécurité** : réservées à root, elles sont lues dans `/var/lib/homebox/status.json`, écrit sans aucun secret toutes les 15 min par `homebox-healthcheck` + `homebox-status` (dossier infra/). Le contrôle de santé y ajoute des seuils soutenus (CPU, RAM, swap, température en moyenne sur 15 min, d'après l'historique) et l'expiration des certificats HTTPS ; il envoie les alertes sur ntfy. Le dashboard peut lancer une sauvegarde ou un contrôle (`sudo systemctl start --no-block …`, seules commandes autorisées avec homebox-firewall).

**Projets** : CPU, mémoire et temps de réponse HTTP de chaque projet en ligne (liste et fiche projet).

**Historique** : un collecteur intégré au dashboard prend une mesure toutes les 15 s dans `<SRV_ROOT>/.data/metrics.db` (7 jours de mesures détaillées, 90 jours de résumés horaires, 1 an de remplissage des partitions). Un verrou garantit un seul collecteur même si le dashboard est lancé deux fois.

**Santé du disque (SMART)** : relevée toutes les heures par `homebox-smart` (dossier infra/, root) dans `/var/lib/homebox/smart.json`, affichée sur la page Système ; les dégradations (secteurs réalloués ou en attente en hausse, auto-test échoué, panne annoncée) déclenchent une alerte ntfy via `homebox-healthcheck`.

Tout est lu sans droits root (psutil, docker, systemctl), sauf le pare-feu qui passe par `homebox-firewall` (dossier infra/, `make tools`). Les mêmes données sont exposées à Neron : `GET /api/v1/system`, `/docker`, `/services`, `/firewall`, `/metrics?range=24h`, `/overview`, `/caddy`.

### Lien vers la tour Devstream Solutions

Si `NEXUS_TOWER_URL` est défini dans `/srv/.env` (par exemple `https://homebox-3.<tailnet>.ts.net:8444`), le menu affiche « Devstream Solutions » et chaque fiche projet un bouton « Voir l'étage » qui ouvre la tour 3D (projet `NexusTower`) directement sur ce projet (`?projet=<nom>`). La valeur est relue à chaque page ; sans elle, les liens n'apparaissent pas. La tour lit en retour `GET /api/v1/projects` avec le jeton de l'API (en-tête `X-Client: nexus-tower`).

### Connexion à l'interface web

L'interface est protégée par un mot de passe unique ; l'API `/api/v1` garde son jeton Bearer et n'est pas concernée.

```bash
make dashboard-password     # définit ou change le mot de passe (saisie masquée)
```

- Seule l'empreinte scrypt est stockée, dans `/srv/.env` (`DASHBOARD_PASSWORD_HASH`), avec le secret de session `DASHBOARD_SESSION_SECRET`.
- Sans mot de passe défini, l'interface reste fermée (page d'instructions), jamais ouverte par défaut.
- Session de 7 jours (cookie `HttpOnly`, `SameSite=Strict`) ; changer le mot de passe déconnecte toutes les sessions.
- 5 échecs en 15 minutes bloquent l'adresse IP pendant le reste de la fenêtre ; chaque connexion (réussie ou non) est notée dans `journalctl -u srv-dashboard`.
- Les formulaires envoyés depuis une autre origine sont refusés (protection CSRF).

### Le faire tourner en permanence (systemd)

`make dashboard` tourne au premier plan et meurt avec la session SSH. L'unité fournie dans `systemd/srv-dashboard.service` le lance au démarrage de homebox et le relance en cas de plantage :

```bash
sudo cp /srv/HomeboxServer/dashboard/systemd/srv-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now srv-dashboard
systemctl status srv-dashboard
journalctl -u srv-dashboard -f      # les logs du dashboard
```

Arrêter le `make dashboard` lancé à la main avant, sinon le port 5199 est déjà pris.

Deux choix volontaires dans l'unité. `KillMode=process` : le dashboard lance les serveurs des projets (`npx serve`, `vite`) via `setsid`/`nohup`, et sans ça un `systemctl restart srv-dashboard` les tuerait tous — avec, seul le dashboard est redémarré, tes sites restent en ligne. Et le durcissement reste léger (`ProtectSystem=full`, `NoNewPrivileges`, pas de `ProtectHome`) parce que `git`, `gh`, `pnpm`, `npx` et `vercel` écrivent dans le HOME d'eleazar — un `ProtectHome` casserait les déploiements.

Le jeton n'est pas injecté dans l'environnement du service : il est relu dans `/srv/.env` à chaque appel, donc `make api-token ARGS=--rotate` prend effet sans redémarrer quoi que ce soit.

Changer le port : `sudo systemctl edit srv-dashboard` puis `[Service]` / `Environment=DASHBOARD_PORT=5195`.

## API pour Neron

Base : `http://127.0.0.1:5199/api/v1` depuis homebox. Documentation interactive sur `/docs`, schéma machine sur `/openapi.json`, résumé lisible sur la page « API & Neron » du dashboard.

**Jeton** — l'API exige un Bearer, l'interface web non :

```bash
make api-token                 # génère DASHBOARD_API_TOKEN dans /srv/.env et l'affiche
make api-token ARGS=--rotate   # en génère un nouveau
```

Tant qu'aucun jeton n'est configuré, l'API répond `503` (jamais ouverte par défaut). Le fichier `.env` est relu à chaque appel : une rotation ne demande pas de redémarrage. L'en-tête facultatif `X-Client: neron` est repris dans la colonne « source » du journal, ce qui distingue les actions de Neron de celles lancées à la main.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/health` | ping — seul appel sans jeton |
| GET | `/summary` | compteurs + activité des 14 derniers jours |
| GET | `/projects` | liste + état live (port, git, vercel), filtres `status` et `root` |
| GET | `/projects/{nom}` | fiche complète : commits, déploiements, journal |
| POST | `/projects` | créer un projet |
| PATCH | `/projects/{nom}` | statut, priorité, description, notes |
| POST | `/projects/{nom}/actions` | start · stop · restart · dev · build · test · git-push · vercel-link · deploy · deploy-prod |
| GET | `/projects/{nom}/logs` | fin du log du service |
| GET | `/activity`, `/activity/{id}` | journal, et sortie complète d'une action |
| GET | `/deployments` | historique Vercel avec URLs |
| POST | `/sync` | scanner `/srv`, enregistrer les nouveaux dossiers et retirer les projets dont le dossier a disparu |

`POST /actions` accepte `{"action": "deploy", "wait": false}` : la réponse est immédiate (`202`) avec une activité `running` à suivre via `/activity/{id}`, ou `wait: true` pour attendre la fin (`200`). Codes d'erreur utiles : `409` action déjà en cours sur ce projet, `400` projet non éligible Vercel, `404` projet inconnu, `422` action ou champ invalide.

**Client Python** — `clients/srv_dashboard_client.py` (dans ce dépôt), sans aucune dépendance (urllib), à déposer dans Neron :

```python
from srv_dashboard_client import SrvDashboardClient

srv = SrvDashboardClient("http://127.0.0.1:5199/api/v1", token=os.environ["DASHBOARD_API_TOKEN"])
srv.summary()                                 # {'total': 11, 'up': 3, ...}
srv.projects(status="active")
activity = srv.run_action("duoLingo", "deploy")
srv.wait_for(activity["id"])                  # sonde jusqu'à ok/error
srv.update_project("duoLingo", status="paused", notes="en attente des maquettes")
```

## Journal (SQLite)

`<racine>/.data/dashboard.db` (donc `/srv/.data/dashboard.db`), deux tables : `activity` (début, fin, projet, action, source, résultat, code de sortie, durée, sortie tronquée à 20 000 caractères) et `deployments` (projet, environnement, résultat, URL Vercel extraite de la sortie). Connexion par opération, `WAL` + `busy_timeout` + `foreign_keys` activés dès l'ouverture. Au démarrage du dashboard, une action restée `running` (processus tué en cours de route) est clôturée en erreur plutôt que de rester en suspens.

## Ce qui a été testé, et comment

123 vérifications automatiques dans un `/srv` factice, avec `gh` et `vercel` simulés : rendu des quatre pages, création de projet (web + API), suivi persistant dans le registre, actions synchrones et asynchrones, refus des actions hors liste blanche et des actions concurrentes sur un même projet, extraction de l'URL Vercel, journal et filtres, authentification (503 sans jeton, 401 sans en-tête, 403 mauvais jeton), génération et rotation du jeton, et le client Python contre un vrai serveur uvicorn. Le rendu a été relu en capture d'écran en thème clair, sombre et sur mobile, et l'unité systemd passe `systemd-analyze verify`.

Une régression est explicitement couverte : `make start` lance le serveur d'un projet en arrière-plan, et le sous-shell qui le porte garde le tube de sortie ouvert tant que le serveur tourne. La sortie des commandes passe donc par un fichier temporaire et jamais par un tube — sinon l'action resterait « en cours » indéfiniment et le projet verrouillé.

En revanche, **aucun compte réel n'a été touché** : les vraies commandes `git init`, `gh repo create`, `vercel link` et `vercel deploy` n'ont jamais été exécutées depuis l'environnement de développement. C'est le premier `make sync` / premier deploy réel qui les éprouve ; le message affiché après chaque étape dit clairement si elle a réussi.
