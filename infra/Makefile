# ==========================================================
# HOMEBOX : raccourcis de la partie infra/ (voir aussi le Makefile à la racine)
# Usage : make            (liste des commandes)
#         make <cible> [VARIABLE=valeur]
# ==========================================================

SHELL        := /bin/bash
.DEFAULT_GOAL := help

DOCKER_DIR   := /srv/docker
COMPOSE      := docker compose --project-name homebox --env-file $(DOCKER_DIR)/.env -f $(DOCKER_DIR)/compose.yml

# Valeurs par défaut des variables
BACKUP       ?= latest
SERVICE      ?=
NAME         ?=
DOMAIN       ?=
PORT         ?=
DIR          ?=

# Vérifie qu'une variable est renseignée : $(call require,NAME,make project-create NAME=monprojet)
require = $(if $($(1)),,$(error $(1) manquant. Exemple : $(2)))

.PHONY: help \
        install-node install tools upgrade firewall firewall-check harden-ssh remove-system-db \
        status ps logs health timers tunnel \
        backup backups verify restore-test restore-postgres restore-redis \
        offsite-init offsite-status snapshots offsite-check \
        notify-test update update-check \
        project-create project-list project-password project-drop \
        site-add site-static site-list site-remove \
        lint

help: ## Affiche cette aide
	@echo "HOMEBOX : commandes disponibles"
	@echo ""
	@awk 'BEGIN {FS = ":.*## "} \
		/^# --- / { sub(/^# --- /, ""); sub(/ -+$$/, ""); printf "\n\033[1m%s\033[0m\n", $$0 } \
		/^[a-zA-Z_-]+:.*## / { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# --- Installation ---------------------------------------------------

install-node: ## Prépare une machine neuve (outils, Docker, Caddy, pare-feu)
	sudo bash ./install-node.sh

install: ## Installe ou met à jour l'infrastructure Docker (relançable)
	sudo bash ./infra-docker.sh

tools: ## Met à jour les outils, sudoers et tâches planifiées (sans toucher aux conteneurs)
	sudo bash ./infra-docker.sh --tools-only

upgrade: remove-system-db install ## Mise à niveau d'une machine existante (doublons + infra)
	@echo ""
	@echo "Étapes suivantes :"
	@echo "  make harden-ssh     (après avoir ajouté ta clé : ssh-copy-id depuis ton poste)"
	@echo "  make notify-test    (après t'être abonné au sujet ntfy)"
	@echo "  make offsite-init   (après avoir rempli /etc/homebox/restic.env)"
	@echo "  make update"

firewall: ## Active le pare-feu UFW (réseau local + Tailscale de confiance, Internet bloqué)
	sudo bash ./firewall.sh

firewall-check: ## Affiche les règles du pare-feu sans les appliquer
	sudo bash ./firewall.sh --dry-run

harden-ssh: ## SSH par clés uniquement (refuse si aucune clé autorisée)
	sudo bash ./harden-ssh.sh

remove-system-db: ## Sauvegarde puis désactive PostgreSQL/Redis système (doublons)
	sudo bash ./remove-system-db.sh

# --- État ------------------------------------------------------------

status: ## Conteneurs, tâches planifiées et santé
	@$(COMPOSE) ps
	@echo ""
	@systemctl list-timers 'homebox-*' --no-pager
	@echo ""
	@sudo homebox-healthcheck --no-notify

ps: ## Liste des conteneurs HOMEBOX
	@$(COMPOSE) ps

logs: ## Logs en direct (SERVICE=postgres|redis|adminer|redis-commander|uptime-kuma)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

health: ## Contrôle de santé immédiat (sans notification)
	@sudo homebox-healthcheck --no-notify

timers: ## Prochaines exécutions des tâches planifiées
	@systemctl list-timers 'homebox-*' --no-pager

tunnel: ## Affiche la commande SSH pour accéder aux interfaces web
	@echo "Depuis ton poste :"
	@echo "  ssh -L 8080:localhost:8080 -L 8081:localhost:8081 -L 3001:localhost:3001 $(USER)@homebox"
	@echo "Puis : Adminer http://localhost:8080 · Redis Commander http://localhost:8081 · Uptime Kuma http://localhost:3001"

# --- Sauvegardes et restauration -------------------------------------

backup: ## Lance une sauvegarde immédiate (locale + externe)
	sudo systemctl start homebox-backup.service
	@sudo journalctl -u homebox-backup.service -n 8 --no-pager -o cat

backups: ## Liste les sauvegardes locales
	@sudo homebox-restore list

verify: ## Vérifie l'intégrité d'une sauvegarde (BACKUP=latest|horodatage)
	sudo homebox-restore verify $(BACKUP)

restore-test: ## Restauration à blanc, sans risque (BACKUP=latest|horodatage)
	sudo homebox-restore test $(BACKUP)

restore-postgres: ## Restaure PostgreSQL : REMPLACE les données (BACKUP=..., NAME=base optionnel)
	sudo homebox-restore postgres $(BACKUP) $(NAME)

restore-redis: ## Restaure Redis : REMPLACE les données (BACKUP=...)
	sudo homebox-restore redis $(BACKUP)

# --- Copie externe (restic) ------------------------------------------

offsite-init: ## Crée le dépôt restic (après avoir rempli /etc/homebox/restic.env)
	sudo homebox-offsite init

offsite-status: ## État de la copie externe
	@sudo homebox-offsite status

snapshots: ## Liste les copies externes
	sudo homebox-offsite snapshots

offsite-check: ## Vérifie l'intégrité du dépôt externe
	sudo homebox-offsite check

# --- Maintenance ------------------------------------------------------

notify-test: ## Envoie une notification de test (ntfy)
	sudo homebox-notify --test

update-check: ## Mises à jour d'images disponibles ?
	sudo homebox-update --check

update: ## Sauvegarde puis applique les mises à jour d'images
	sudo homebox-update

lint: ## Vérifie la syntaxe de tous les scripts du kit
	@for f in *.sh files/bin/* files/lib/common.sh; do \
		[ -f "$$f" ] || continue; \
		if head -n1 "$$f" | grep -q python; then \
			python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$$f"; \
		else \
			bash -n "$$f"; \
		fi && echo "ok      $$f" || { echo "ÉCHEC   $$f"; exit 1; }; \
	done

# --- Projets (bases PostgreSQL) --------------------------------------

project-create: ## Crée une base + un utilisateur (NAME=monprojet)
	$(call require,NAME,make project-create NAME=monprojet)
	homebox-project create $(NAME)

project-list: ## Liste les bases des projets
	@homebox-project list

project-password: ## Nouveau mot de passe pour un projet (NAME=monprojet)
	$(call require,NAME,make project-password NAME=monprojet)
	homebox-project password $(NAME)

project-drop: ## Supprime la base d'un projet, avec confirmation (NAME=monprojet)
	$(call require,NAME,make project-drop NAME=monprojet)
	homebox-project drop $(NAME)

# --- Sites web (Caddy) -----------------------------------------------

site-add: ## Publie un port en HTTPS (DOMAIN=app.exemple.fr PORT=5190)
	$(call require,DOMAIN,make site-add DOMAIN=app.exemple.fr PORT=5190)
	$(call require,PORT,make site-add DOMAIN=app.exemple.fr PORT=5190)
	sudo homebox-site add $(DOMAIN) $(PORT)

site-static: ## Publie un dossier statique (DOMAIN=doc.exemple.fr DIR=/srv/Projects/doc/dist)
	$(call require,DOMAIN,make site-static DOMAIN=doc.exemple.fr DIR=/chemin/dist)
	$(call require,DIR,make site-static DOMAIN=doc.exemple.fr DIR=/chemin/dist)
	sudo homebox-site add $(DOMAIN) --static $(DIR)

site-list: ## Liste les sites publiés
	@sudo homebox-site list

site-remove: ## Retire un site (DOMAIN=app.exemple.fr)
	$(call require,DOMAIN,make site-remove DOMAIN=app.exemple.fr)
	sudo homebox-site remove $(DOMAIN)
