# HomeboxServer

Tout ce qui fait tourner et surveille le serveur **homebox** (Ubuntu 26.04) :
installation, bases de données en conteneurs, sauvegardes, pare-feu, santé du
disque, et un dashboard web pour suivre et piloter le tout.

```
/srv/HomeboxServer/
├── Makefile      point d'entrée unique (make help)
├── docs/         documentation du serveur (serveur.md)
├── infra/        partie root : installation, Docker (PostgreSQL, Redis…), outils homebox-*,
│                 sauvegardes locales et externes, pare-feu, SSH, SMART, contrôle de santé
└── dashboard/    application web (FastAPI) : vue d'ensemble, système, historique,
                  Docker, services, sauvegardes, pare-feu, projets ; API pour Neron
```

## Comment les deux parties travaillent ensemble

Le dashboard tourne sous un utilisateur normal ; ce qui demande root vit dans `infra/`.

| Dans le dashboard | Fourni par infra/ |
|---|---|
| Page Pare-feu | `homebox-firewall` (valide tout) + `/etc/sudoers.d/homebox-firewall` |
| Page Caddy | `homebox-caddy` (valide tout, remet l'ancienne configuration si Caddy la refuse), même règle sudo |
| Boutons « Sauvegarder maintenant », « Actualiser l'état » | deux commandes `systemctl` autorisées par la même règle sudo |
| Vue d'ensemble, Sauvegardes (alertes, sécurité, certificats) | `/var/lib/homebox/status.json`, écrit sans secret par `homebox-status` |
| Carte « Santé du disque » | `/var/lib/homebox/smart.json`, écrit par `homebox-smart` |
| Docker, bases des projets | conteneurs `homebox-*`, `homebox-project` |

Dans l'autre sens, le contrôle de santé (`homebox-healthcheck`) lit l'historique du
dashboard (`/srv/.data/metrics.db`) pour ses alertes de seuils soutenus.

## Mise en service

Machine neuve :

```bash
cd /srv && git clone git@github.com:enabeteleazar/HomeboxServer.git
ln -s HomeboxServer/dashboard/Makefile /srv/Makefile   # « cd /srv && make list »
cd /srv/HomeboxServer
make install-node        # outils de dev, Docker, Caddy, pare-feu (sudo)
make install             # conteneurs, sauvegardes, outils homebox-*, tâches planifiées
make dashboard-password  # mot de passe de l'interface web
make setup               # venv + service du dashboard
make firewall            # pare-feu (réseau local + Tailscale de confiance, Internet bloqué)
```

Après une mise à jour du dépôt (`git pull`) :

```bash
make tools               # outils root, sudoers, tâches planifiées (sans toucher aux conteneurs)
make dashboard-restart
```

Puis `http://homebox:5199/overview` (réseau local ou Tailscale).

## Documentation détaillée

- [docs/serveur.md](docs/serveur.md) : **la machine** — matériel, partitions, organisation
  de `/srv`, versions, Docker, ports, sécurité, sauvegardes, surveillance, points en attente
  (aussi accessible par `/srv/README.md`).
- [infra/README.md](infra/README.md) : installation, sauvegardes et restauration, copie
  externe, pare-feu, SSH, SMART, alertes ntfy, mises à jour des conteneurs.
- [dashboard/README.md](dashboard/README.md) : pages, registre des projets, API pour Neron,
  connexion, historique.
