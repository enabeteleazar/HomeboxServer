# ==========================================================
# HOMEBOX SERVER : point d'entrée unique
#   infra/      scripts root, outils homebox-*, sauvegardes, pare-feu
#   dashboard/  interface web de suivi et de pilotage
# Usage : make            (liste des commandes)
# Les commandes de gestion des projets (make list, make dev <projet>…) restent
# dans /srv : /srv/Makefile -> HomeboxServer/dashboard/Makefile.
# ==========================================================

SHELL         := /bin/bash
.DEFAULT_GOAL := help

ROOT_DIR      := $(patsubst %/,%,$(dir $(realpath $(firstword $(MAKEFILE_LIST)))))
DASH_DIR      := $(ROOT_DIR)/dashboard
UNIT          := srv-dashboard.service

# Cibles transmises telles quelles à infra/Makefile (variables comprises)
INFRA_TARGETS := install-node install tools upgrade firewall firewall-check harden-ssh \
                 remove-system-db status ps logs health timers tunnel \
                 backup backups verify restore-test restore-postgres restore-redis \
                 offsite-init offsite-status snapshots offsite-check \
                 notify-test update-check update \
                 project-create project-list project-password project-drop \
                 site-add site-static site-list site-remove

# Cibles transmises à dashboard/Makefile
DASH_TARGETS  := dashboard-password api-token sync list

.PHONY: help setup dashboard-venv dashboard-unit dashboard-restart dashboard-logs lint \
        $(INFRA_TARGETS) $(DASH_TARGETS)

help: ## Affiche cette aide
	@echo "HOMEBOX SERVER"
	@echo ""
	@echo "Mise en service :"
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "Infrastructure (infra/) :"
	@echo "  $(INFRA_TARGETS)" | fold -s -w 78 | sed 's/^/  /'
	@echo "  → détail : make -C infra help"
	@echo ""
	@echo "Dashboard (dashboard/) : $(DASH_TARGETS)"
	@echo "Projets : cd /srv && make list"

setup: tools dashboard-venv dashboard-unit ## Tout mettre en service (outils root, venv, service du dashboard)
	@echo ""
	@echo "Si ce n'est pas fait : make dashboard-password, puis ouvre http://homebox:5199/overview"

dashboard-venv: ## (Re)crée l'environnement Python du dashboard
	python3 -m venv "$(DASH_DIR)/.venv"
	"$(DASH_DIR)/.venv/bin/pip" install -q -r "$(DASH_DIR)/requirements.txt"

dashboard-unit: ## Installe l'unité systemd du dashboard et le redémarre
	sudo install -m 0644 "$(DASH_DIR)/systemd/$(UNIT)" /etc/systemd/system/$(UNIT)
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(UNIT)
	sudo systemctl restart $(UNIT)
	@systemctl --no-pager --lines=0 status $(UNIT) | head -3

dashboard-restart: ## Redémarre le dashboard (après une mise à jour du code)
	sudo systemctl restart $(UNIT)

dashboard-logs: ## Journal du dashboard en direct
	journalctl -u $(UNIT) -f

lint: ## Vérifie la syntaxe des scripts (infra) et du code Python (dashboard)
	@$(MAKE) --no-print-directory -C infra lint
	@cd "$(DASH_DIR)" && for f in *.py srvctl/*.py; do \
		python3 -m py_compile "$$f" && echo "ok      dashboard/$$f" || exit 1; \
	done

$(INFRA_TARGETS):
	@$(MAKE) --no-print-directory -C infra $@

$(DASH_TARGETS):
	@$(MAKE) --no-print-directory -C "$(DASH_DIR)" $@
